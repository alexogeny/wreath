"""Quiet the machine before a benchmark, and guarantee it comes back.

Benchmark numbers on a desktop are dominated by things that have nothing to do
with the code under test: a frequency governor that ramps, an SMT sibling
running a browser, a file indexer waking up mid-run. This module removes those
in tiers, and the tier you should use is the lowest one that gets the variance
you need -- which is a measurement, not a preference. `--measure-tiers` reports
the A/A spread at each tier so the answer is evidence rather than habit.

    Tier 0  no privileges, always safe. SMT-aware core allocation, niceness,
            and ASLR disabled for the benchmark children.
    Tier 1  sudo, fully reversible, every prior value recorded. CPU governor,
            turbo, transparent huge pages, a *named* list of background
            services stopped, *named* heavy applications frozen, and running
            containers paused.
    Tier 2  opt-in, last resort. Freezes every transient application scope,
            named or not.

Tier 1 carries the named lists and tier 2 carries the sweep, because that is the
distinction that decides whether an operator can audit what is about to happen.
A browser and a Postgres container are exactly as noisy as a file indexer, and
`NOISY_SERVICES` already established that a *named* list is the safe way to stop
background work -- so containers and heavy applications belong beside it rather
than behind the broad freeze nobody should need.

**Nothing here kills anything.** Tier 2 uses cgroup v2 `cgroup.freeze`, which
suspends a process tree and resumes it exactly as it was; a killed desktop has
to be respawned and loses your windows, a frozen one does not.

Three properties make this safe to hand a machine you are sitting in front of:

* **The restorer is armed before the first change.** A detached systemd timer
  restores everything after a deadline whether or not the benchmark survives --
  crash, `SIGKILL`, closing the terminal, walking away. `arm()` refuses to make
  any change it cannot prove it can undo.
* **The benchmark's own ancestry is exempt.** `session_ancestry()` walks up from
  this process's cgroup and every ancestor is excluded from freezing, so
  `--quiet` can never suspend the terminal it was launched from. On a GNOME
  desktop the benchmark runs *inside* the same `app.slice` as the terminal, so
  a naive "freeze the user session" would take the operator's shell with it.
* **Every change is journalled to disk** before it is applied, so `--restore`
  works from a fresh shell after any failure short of a reboot -- and a reboot
  restores all of this anyway.

A fourth property guards the *result* rather than the machine: `apply()` refuses
to quiet anything while another benchmark, test run or agent is executing, because
a number measured alongside four other processes is not a number. See
`competing_workloads()`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_journal() -> Path:
    """Where the journal lives: `/run` as root, `/tmp` otherwise.

    Not the repository, either way -- it describes machine state, not project
    state, and a stale one left in a working tree would be committed by accident.

    The split exists because `/tmp` cannot hold a file two uids take turns
    writing. It is world-writable and sticky, and under `fs.protected_regular`
    (2 on Debian) the kernel refuses to open a file there that the opener does
    not own -- *including* for root, which gets no capability exemption from that
    check. So a tier-0 run as you, followed by the `sudo` run tier 1 requires,
    failed on the second one with a bare `PermissionError` and a traceback. `/run`
    is root-owned and not sticky, so the process that writes the journal is
    always the one that owns it, and both are tmpfs cleared on reboot -- which is
    when every change here would have been undone anyway.
    """
    override = os.environ.get("WREATH_QUIET_JOURNAL")
    if override:
        return Path(override)
    return Path("/run/wreath-quiet.json" if os.geteuid() == 0 else "/tmp/wreath-quiet.json")


#: The journal of applied changes. See `_default_journal()` for the location.
JOURNAL = _default_journal()

#: The watchdog restores after this long unless the benchmark disarms it first.
#: Long enough for a full battery, short enough that a forgotten freeze thaws
#: itself before anyone reboots in a panic.
DEFAULT_DEADLINE_SECONDS = 1800

#: Background services stopped at tier 1. **Named, never inferred.** A rule like
#: "stop anything idle" is how a benchmark harness takes out a database someone
#: needed. Anything not on this list stays running, including everything whose
#: purpose this module cannot be sure of.
NOISY_SERVICES: tuple[str, ...] = (
    "packagekit.service",
    "snapd.service",
    "fwupd.service",
    "cron.service",
    "anacron.service",
    "man-db.timer",
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "updatedb.timer",
    "plocate-updatedb.timer",
    "tracker-miner-fs-3.service",
    "tracker-extract-3.service",
    "baloo_file.service",
)

#: Heavy desktop applications frozen at tier 1, matched as substrings against a
#: transient scope's name. **Named for the same reason `NOISY_SERVICES` is.**
#: Tier 2 already freezes every application scope, but the measured advice is to
#: stop at tier 1 -- so a browser, which is the single noisiest thing on a
#: developer desktop, was not being quieted in the tier anyone actually runs.
#:
#: Every entry is something a person launched and can see is paused. Nothing here
#: is session plumbing, and `_NEVER_FREEZE` is applied on top regardless, so a
#: careless addition here still cannot take out the desktop.
HEAVY_APPS: tuple[str, ...] = (
    # Browsers -- the usual worst offender, and the reason this list exists.
    "firefox", "chrome", "chromium", "brave", "vivaldi", "opera", "epiphany",
    "microsoft-edge", "zen-browser", "librewolf", "waterfox", "tor-browser",
    # Electron and chat, which idle at a few percent forever.
    "slack", "discord", "element", "signal", "telegram", "whatsapp",
    "teams", "thunderbird", "spotify", "zoom", "skype",
    # Editors and IDEs. Language servers and file watchers are the cost here,
    # not the editor.
    "code", "vscodium", "sublime", "jetbrains", "idea", "pycharm", "webstorm",
    "goland", "clion", "rubymine", "phpstorm", "datagrip", "zed", "cursor",
    # Games and launchers.
    "steam", "lutris", "heroic", "bottles",
    # Sync daemons: periodic, bursty, and invisible in a short run until they
    # land in the middle of one.
    "dropbox", "nextcloud", "syncthing", "insync", "megasync", "onedrive",
    # Update checkers and stores, which wake on a timer. Both spellings: systemd
    # names a GNOME scope from its D-Bus name (`app-gnome-org.gnome.Software-N`),
    # so the hyphenated form alone silently matches nothing.
    "gnome-software", "gnome.software", "discover", "snap-store", "packagekit",
    # Miscellaneous heavyweights.
    "obs", "gimp", "blender", "kdenlive", "darktable", "virtualbox", "virt-manager",
)

#: How a running container is quieted. **`pause`, not `stop`, and the choice is
#: load-bearing** -- see `_CONTAINER_ACTION_REASON`.
CONTAINER_ACTION = os.environ.get("WREATH_QUIET_CONTAINER_ACTION", "pause")

_CONTAINER_ACTION_REASON = {
    "pause": (
        "pause (SIGSTOP via the freezer cgroup): the container burns no CPU while "
        "paused, unpauses in milliseconds, cannot lose data, and -- unlike stop -- "
        "does not destroy a `--rm` container"
    ),
    "stop": (
        "stop (SIGTERM, then SIGKILL): a clean shutdown, but slower to restore and "
        "DESTRUCTIVE for a `--rm` container, which is why those are skipped"
    ),
}

_CPU_ROOT = Path("/sys/devices/system/cpu")
_CGROUP_ROOT = Path("/sys/fs/cgroup")


# --- topology ---------------------------------------------------------------


def physical_cores() -> dict[int, list[int]]:
    """Map each physical core to its logical CPUs, lowest logical CPU first.

    The keys are `core_id` values from sysfs, not indexes, so they are stable
    across a machine that does not number its cores from zero.
    """
    cores: dict[int, list[int]] = {}
    for path in sorted(_CPU_ROOT.glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        siblings = path / "topology" / "thread_siblings_list"
        try:
            cpu = int(path.name[3:])
            listed = siblings.read_text().strip()
        except (OSError, ValueError):
            continue
        members = sorted(_parse_cpu_list(listed))
        if not members:
            continue
        cores.setdefault(members[0], [])
        if cpu not in cores[members[0]]:
            cores[members[0]].append(cpu)
    return {core: sorted(cpus) for core, cpus in cores.items()}


def _parse_cpu_list(text: str) -> set[int]:
    """Parse a sysfs CPU list (`0,6` or `0-3,8`) into a set of CPU numbers."""
    cpus: set[int] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    return cpus


@dataclass(frozen=True, slots=True)
class CoreSplit:
    """A server/generator CPU allocation that never splits a physical core.

    The whole point is `whole_cores`: if the server holds one SMT thread of a
    core and the generator holds the other, they contend for one core's
    execution resources and the benchmark measures that contention. Handing each
    side *entire* cores costs a little parallelism and removes a noise source
    that is invisible in the numbers.
    """

    server: tuple[int, ...]
    client: tuple[int, ...]
    whole_cores: bool
    reason: str


def split_cores(server_cores: int = 1) -> CoreSplit:
    """Allocate whole physical cores to the server, the rest to the generator.

    Falls back to a thread-level split, flagged `whole_cores=False`, when the
    machine has too few cores to give each side one -- better to run and say so
    than to refuse.
    """
    cores = physical_cores()
    if not cores:
        return CoreSplit((), (), False, "sysfs topology unavailable")
    ordered = [cores[key] for key in sorted(cores)]
    if len(ordered) < server_cores + 1:
        flat = [cpu for group in ordered for cpu in group]
        half = max(1, len(flat) // 4)
        return CoreSplit(
            tuple(flat[:half]),
            tuple(flat[half:]),
            False,
            f"only {len(ordered)} physical core(s); split by thread instead",
        )
    server = tuple(cpu for group in ordered[:server_cores] for cpu in group)
    client = tuple(cpu for group in ordered[server_cores:] for cpu in group)
    return CoreSplit(
        server,
        client,
        True,
        f"{server_cores} whole core(s) to the server, "
        f"{len(ordered) - server_cores} to the generator",
    )


# --- containers -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Container:
    """One running container, and whether stopping it would destroy it.

    `auto_remove` is the `--rm` flag. A `--rm` container is *deleted* when it
    stops, so `docker stop` on one is data loss wearing a quiet-down's clothes.
    It is carried here explicitly rather than inferred at the point of use,
    because the whole reason it matters is that the destructive case looks
    identical to the safe one from the outside.

    `unknown_auto_remove` records that the inspect failed. It is treated exactly
    like `auto_remove=True`: when this module cannot tell whether stopping a
    container destroys it, it does not stop it.
    """

    runtime: str
    id: str
    name: str
    image: str
    auto_remove: bool
    unknown_auto_remove: bool = False

    @property
    def destructive_to_stop(self) -> bool:
        return self.auto_remove or self.unknown_auto_remove


def container_runtimes() -> tuple[str, ...]:
    """Which container runtimes are installed. Both may be, and both are used."""
    return tuple(name for name in ("docker", "podman") if shutil.which(name))


def running_containers(*, run: Any = subprocess.run) -> tuple[Container, ...]:
    """Every running container across every installed runtime.

    A failure to enumerate returns nothing rather than raising: a machine with a
    docker binary but no running daemon is the common case, not an error, and a
    benchmark should not refuse to start over it.
    """
    found: list[Container] = []
    for runtime in container_runtimes():
        result = run(
            [runtime, "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if getattr(result, "returncode", 1) != 0:
            continue
        for line in getattr(result, "stdout", "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0].strip():
                continue
            cid, name, image = (part.strip() for part in parts)
            auto, unknown = _auto_remove(runtime, cid, run=run)
            found.append(Container(runtime, cid, name, image, auto, unknown))
    return tuple(found)


def _auto_remove(runtime: str, cid: str, *, run: Any = subprocess.run) -> tuple[bool, bool]:
    """Whether this container is `--rm`, and whether that could be determined.

    Returns `(auto_remove, unknown)`. When the inspect fails the answer is
    `(False, True)`, and every caller must read `unknown` as "assume the worst":
    the default for an undeterminable container is to leave it alone.
    """
    result = run(
        [runtime, "inspect", "--format", "{{.HostConfig.AutoRemove}}", cid],
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        return False, True
    answer = getattr(result, "stdout", "").strip().lower()
    if answer in {"true", "false"}:
        return answer == "true", False
    return False, True


# --- competing workloads, which invalidate the measurement rather than the machine


@dataclass(frozen=True, slots=True)
class Workload:
    """Another process whose presence makes the benchmark's number meaningless."""

    pid: int
    command: str
    why: str


#: Command-line markers for work that contends with a benchmark. Matched against
#: `/proc/<pid>/cmdline`, never against a `pgrep -f` pattern -- `pgrep -f` also
#: matches the shell running the check, so a naive version of this reports a hit
#: every single time and is worse than no check at all.
_COMPETING = (
    ("pytest", "a test run"),
    ("h2load", "another load generator"),
    ("wreath-bench", "another benchmark"),
    ("_devtools.tasks", "another wreath task runner"),
    ("wrk", "another load generator"),
    ("ab ", "another load generator"),
)


def _own_process_tree(pid: int | None = None) -> set[int]:
    """This process and every ancestor, so the check cannot detect itself.

    Without this the checker finds its own Python interpreter under the repo
    path, reports itself as a competing workload, and refuses to run forever.
    """
    tree: set[int] = set()
    current = pid or os.getpid()
    for _ in range(64):  # bounded: a cycle in /proc would otherwise hang us
        if current <= 0 or current in tree:
            break
        tree.add(current)
        try:
            stat = Path(f"/proc/{current}/stat").read_text()
        except OSError:
            break
        # The comm field may contain spaces and parentheses, so parse after the
        # final ')' rather than splitting the whole line.
        tail = stat.rsplit(")", 1)[-1].split()
        if len(tail) < 2:
            break
        try:
            current = int(tail[1])
        except ValueError:
            break
    return tree


#: Executables that constitute *work*. A shell whose working directory happens to
#: be the repository is not a competing workload -- it is an idle prompt, and
#: reporting it teaches the operator to ignore this check. Being in the repo is
#: only interesting when the process is something that burns CPU.
_WORKLOAD_BINARIES = frozenset(
    {
        "python", "python3", "python3.14", "pypy", "pypy3",
        "node", "deno", "bun",
        "cc", "cc1", "cc1plus", "gcc", "g++", "clang", "clang++", "ld", "lto1",
        "make", "ninja", "cmake", "cargo", "rustc", "go", "java", "javac",
        "ruff", "ty", "mypy", "pyright",
    }
)


def _proc_link(entry: Path, name: str) -> str:
    """Resolve `/proc/<pid>/exe` or `/proc/<pid>/cwd`, or "" if unreadable."""
    try:
        return os.readlink(entry / name)
    except OSError:
        return ""


def _is_workload(exe: str) -> bool:
    """Whether this executable is the kind of thing that competes for a core."""
    name = exe.rsplit("/", 1)[-1]
    if name in _WORKLOAD_BINARIES:
        return True
    # `python3.14`, `python3.14t` (free-threaded) and friends: version-suffixed
    # names are matched by prefix so a new point release is not a blind spot.
    return name.startswith(("python", "pypy"))


def competing_workloads(
    *, pid: int | None = None, repo: Path | None = None
) -> tuple[Workload, ...]:
    """Processes that would contaminate a measurement taken right now.

    Finds other test runs, load generators and benchmark invocations, plus any
    other process whose executable or working directory is inside this
    repository -- which is how an agent, a worktree build or a stray script
    shows up. Excludes this process and its whole ancestry, so it can never
    find itself.

    **Association with the repository is decided from `/proc/<pid>/exe` and
    `/proc/<pid>/cwd`, never from the command line.** The first version matched
    the repo path as a substring of the command line and missed a process
    launched as `.venv/bin/python` from the repo root, because that command line
    contains no absolute path at all. A check with a hole that shape reports an
    idle machine while an agent is running, which is the exact failure it exists
    to prevent.

    Returning nothing is what lets a benchmark proceed, so the self-exclusion is
    equally load-bearing in the other direction: a check that always fires gets
    overridden by habit, and then it is not a check.
    """
    exempt = _own_process_tree(pid)
    root = str(repo or Path(__file__).resolve().parents[3])
    # A sibling worktree is a different checkout of the same project and its
    # builds contend just as hard, so match the family rather than one path.
    family = root.rsplit("/", 1)[-1].split("-")[0]
    parent_dir = root.rsplit("/", 1)[0]
    found: list[Workload] = []
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        candidate = int(entry.name)
        if candidate in exempt:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if not command:
            continue  # a kernel thread
        why = ""
        for marker, reason in _COMPETING:
            if marker in command:
                why = reason
                break
        if not why:
            exe = _proc_link(entry, "exe")
            if _is_workload(exe):
                cwd = _proc_link(entry, "cwd")
                here = exe.startswith(root + "/") or cwd == root or cwd.startswith(root + "/")
                sibling = exe.startswith(f"{parent_dir}/{family}-") or cwd.startswith(
                    f"{parent_dir}/{family}-"
                )
                if here:
                    why = "another process running out of this repository"
                elif sibling:
                    why = "a process running out of a sibling worktree"
        if why:
            found.append(Workload(candidate, command[:160], why))
    return tuple(found)


# --- the benchmark's own ancestry, which must never be frozen ----------------


def session_ancestry(pid: int | None = None) -> tuple[str, ...]:
    """Every cgroup path from this process's own cgroup up to the root.

    Freezing any of these suspends the benchmark, and on a desktop it suspends
    the terminal too: a GNOME session puts `vte-spawn-*.scope` under
    `app-org.gnome.Terminal.slice`, itself under `app.slice` and
    `user@1000.service`. "Freeze the user session" therefore includes the
    operator's shell. This is the list that makes that impossible.
    """
    source = Path(f"/proc/{pid or 'self'}/cgroup")
    try:
        raw = source.read_text()
    except OSError:
        return ()
    own = ""
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            own = parts[2]
            break
    if not own:
        return ()
    ancestry: list[str] = []
    current = own
    while True:
        ancestry.append(current)
        if current in {"", "/"}:
            break
        current = current.rsplit("/", 1)[0] or "/"
    return tuple(ancestry)


#: Session infrastructure that is a *sibling* of the benchmark, not an ancestor,
#: and still must never be frozen. Excluding only ancestors is not enough: the
#: terminal survives a frozen `dbus.socket` for exactly as long as it takes to
#: make its next D-Bus call, and then blocks forever. The operator loses their
#: shell without a single ancestor having been touched.
#:
#: Same reasoning for the rest: freezing an ssh-agent hangs the next `git push`,
#: freezing the accessibility bus hangs GTK, and freezing the session manager
#: takes the desktop's supervisor with it.
_NEVER_FREEZE = (
    "session-manager",
    "keyring",
    "ssh-agent",
    "gpg-agent",
    "dbus",
    "portal",
    "at-spi",
    "dconf",
)


def freezable_targets(pid: int | None = None) -> tuple[Path, ...]:
    """User applications that are safe to freeze -- an allowlist by shape.

    Returns only *transient application scopes* (`app-*.scope`) under the user's
    app slice: things a person launched, like a browser or a music player.
    Sockets, services and slices are session infrastructure and are excluded
    wholesale, because the failure mode of guessing wrong is the operator losing
    their desktop.

    An empty result is a legitimate answer meaning "nothing here is safe to
    freeze", and must never be read as permission to freeze more broadly.
    """
    exempt = set(session_ancestry(pid))
    if not exempt:
        return ()
    app_slice = next((path for path in exempt if path.endswith("/app.slice")), None)
    if app_slice is None:
        return ()
    root = _CGROUP_ROOT / app_slice.lstrip("/")
    if not root.is_dir():
        return ()
    app_prefix = app_slice.rstrip("/") + "/"
    protected_children = {
        app_prefix + suffix.partition("/")[0]
        for ancestor in exempt
        if ancestor.startswith(app_prefix)
        and (suffix := ancestor.removeprefix(app_prefix))
    }
    targets: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.endswith(".scope"):
            continue
        relative = "/" + str(child.relative_to(_CGROUP_ROOT))
        if relative in protected_children:
            continue
        lowered = child.name.lower()
        if any(banned in lowered for banned in _NEVER_FREEZE):
            continue
        if (child / "cgroup.freeze").exists():
            targets.append(child)
    return tuple(targets)


def named_app_targets(pid: int | None = None) -> tuple[Path, ...]:
    """The subset of `freezable_targets()` matching a name in `HEAVY_APPS`.

    Deliberately a *filter over* `freezable_targets()` rather than its own walk
    of the cgroup tree. Every safety property -- the ancestry exemption, the
    `_NEVER_FREEZE` denylist, the transient-scope-only shape check -- is applied
    first and applies here unchanged. A second enumeration would be a second
    place for those to be got wrong, and only one of them would have the tests.
    """
    return tuple(
        target
        for target in freezable_targets(pid)
        if any(app in target.name.lower() for app in HEAVY_APPS)
    )


# --- changes and the journal ------------------------------------------------


@dataclass(slots=True)
class Change:
    """One reversible modification, carrying everything needed to undo it.

    `kind` selects the restore strategy, `target` names what was changed, and
    `previous` is the value read *before* the change. A `Change` is journalled
    before it is applied, never after, so a crash between the two leaves an
    instruction to restore something that was not changed -- which is harmless,
    because every restore path is idempotent -- rather than a change with no
    instruction, which is not.
    """

    kind: str
    target: str
    previous: str
    desired: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "target": self.target,
            "previous": self.previous,
            "desired": self.desired,
        }


@dataclass(slots=True)
class Journal:
    """The on-disk record of what was changed, and what restores it."""

    changes: list[Change] = field(default_factory=list)
    armed_at: float = 0.0
    deadline: float = 0.0
    watchdog: str = ""

    def write(self, path: Path = JOURNAL) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "armed_at": self.armed_at,
            "deadline": self.deadline,
            "watchdog": self.watchdog,
            "changes": [change.as_dict() for change in self.changes],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")

    @classmethod
    def read(cls, path: Path = JOURNAL) -> Journal:
        try:
            payload: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        return cls(
            changes=[Change(**row) for row in payload.get("changes", [])],
            armed_at=float(payload.get("armed_at", 0.0)),
            deadline=float(payload.get("deadline", 0.0)),
            watchdog=str(payload.get("watchdog", "")),
        )


# --- restore ----------------------------------------------------------------


def restore(path: Path = JOURNAL, *, run: Any = subprocess.run) -> list[str]:
    """Undo every journalled change, in reverse order. Idempotent by design.

    Restoring an already-restored machine is a clean no-op that returns an empty
    list: each strategy writes the previous value unconditionally rather than
    checking first, so a partially-applied journal and a fully-applied one
    restore identically. Returns a human-readable line per restored change.
    """
    journal = Journal.read(path)
    if not journal.changes:
        return []
    restored: list[str] = []
    for change in reversed(journal.changes):
        try:
            _restore_one(change, run)
        except OSError as error:
            restored.append(f"FAILED {change.kind} {change.target}: {error}")
            continue
        restored.append(f"restored {change.kind} {change.target} -> {change.previous}")
    path.unlink(missing_ok=True)
    return restored


def _restore_one(change: Change, run: Any) -> None:
    """Apply one change's inverse. Every branch is safe to run twice."""
    if change.kind == "sysfs":
        Path(change.target).write_text(change.previous)
    elif change.kind == "freeze":
        node = Path(change.target)
        if node.exists():
            node.write_text(change.previous)
    elif change.kind == "service":
        if change.previous == "active":
            run(["systemctl", "start", change.target], check=False)
    elif change.kind == "governor":
        for policy in _CPU_ROOT.glob("cpu[0-9]*/cpufreq/scaling_governor"):
            policy.write_text(change.previous)
    elif change.kind in {"container-pause", "container-stop"}:
        # `target` is "<runtime>\t<id>\t<name>". The id is what the runtime is
        # asked about, and the name is carried so a human reading the journal
        # can tell what a bare hex id was.
        runtime, _, rest = change.target.partition("\t")
        cid = rest.partition("\t")[0]
        verb = "unpause" if change.kind == "container-pause" else "start"
        run([runtime, verb, cid], check=False, capture_output=True)


# --- the plan ---------------------------------------------------------------


@dataclass(slots=True)
class Step:
    """One planned action, with the command a human would run to do it by hand.

    `command` exists so `--dry-run` can print something the operator can read,
    check, and run themselves. A plan nobody can audit is a plan nobody should
    grant sudo to.
    """

    tier: int
    description: str
    command: str
    change: Change | None = None


def plan(tier: int, *, pid: int | None = None) -> list[Step]:
    """Everything `--quiet=tier` would do, in order, without doing any of it."""
    steps: list[Step] = []
    if tier >= 0:
        split = split_cores()
        steps.append(
            Step(
                0,
                f"pin server to CPUs {list(split.server)}, generator to "
                f"{list(split.client)} ({split.reason})",
                "sched_setaffinity(2) -- no shell command, applied in-process",
            )
        )
        steps.append(
            Step(0, "disable ASLR for benchmark children", "setarch -R <child>")
        )
        steps.append(Step(0, "renice the benchmark tree to -5", "renice -n -5 -p $$"))
    if tier >= 1:
        for policy in sorted(_CPU_ROOT.glob("cpu[0-9]*/cpufreq/scaling_governor")):
            current = _read(policy)
            if current and current != "performance":
                steps.append(
                    Step(
                        1,
                        f"{policy}: {current} -> performance",
                        f"echo performance | sudo tee {policy}",
                        Change("sysfs", str(policy), current, "performance"),
                    )
                )
        boost = _CPU_ROOT / "cpufreq" / "boost"
        current = _read(boost)
        if current == "1":
            steps.append(
                Step(
                    1,
                    "disable turbo so frequency cannot wander mid-run",
                    f"echo 0 | sudo tee {boost}",
                    Change("sysfs", str(boost), current, "0"),
                )
            )
        thp = Path("/sys/kernel/mm/transparent_hugepage/enabled")
        current = _read(thp)
        if current and "[always]" in current:
            steps.append(
                Step(
                    1,
                    "transparent huge pages: always -> madvise",
                    f"echo madvise | sudo tee {thp}",
                    Change("sysfs", str(thp), "always", "madvise"),
                )
            )
        paranoid = Path("/proc/sys/kernel/perf_event_paranoid")
        current = _read(paranoid)
        if current and current != "-1":
            steps.append(
                Step(
                    1,
                    f"perf_event_paranoid: {current} -> -1",
                    "sudo sysctl -w kernel.perf_event_paranoid=-1",
                    Change("sysfs", str(paranoid), current, "-1"),
                )
            )
        for service, state in zip(
            NOISY_SERVICES,
            _service_states(NOISY_SERVICES),
            strict=True,
        ):
            if state == "active":
                steps.append(
                    Step(
                        1,
                        f"stop {service} (was active)",
                        f"sudo systemctl stop {service}",
                        Change("service", service, "active", "stopped"),
                    )
                )
        steps.extend(_container_steps())
        for target in named_app_targets(pid):
            node = target / "cgroup.freeze"
            current = _read(node) or "0"
            steps.append(
                Step(
                    1,
                    f"freeze application {_scope_label(target.name)}",
                    f"echo 1 > {node}",
                    Change("freeze", str(node), current, "1"),
                )
            )
    if tier >= 2:
        targets = freezable_targets(pid)
        exempt = session_ancestry(pid)
        steps.append(
            Step(
                2,
                f"exempt {len(exempt)} ancestor cgroup(s) of this process: "
                f"{exempt[0] if exempt else '(none)'}",
                "# no command -- this is the safety check, not an action",
            )
        )
        already = set(named_app_targets(pid))
        for target in targets:
            if target in already:
                continue  # tier 1 froze this one by name; do not journal it twice
            node = target / "cgroup.freeze"
            current = _read(node) or "0"
            steps.append(
                Step(
                    2,
                    f"freeze {_scope_label(target.name)} (unnamed application)",
                    f"echo 1 > {node}",
                    Change("freeze", str(node), current, "1"),
                )
            )
    return steps


def _scope_label(name: str) -> str:
    """A cgroup scope name a person can read.

    systemd escapes characters in unit names (`at\\x2dspi` for `at-spi`), which
    makes an unprocessed plan harder to audit than it needs to be.
    """
    readable = name.removesuffix(".scope").removeprefix("app-")
    return readable.replace("\\x2d", "-")


def _container_steps(*, run: Any = subprocess.run) -> list[Step]:
    """Pause (or stop) every running container, skipping the ones that would die.

    The `--rm` skip is the reason this is not a one-liner. `docker stop` on a
    container started with `--rm` *deletes* it, so a harness that stops
    everything to get a quiet machine can silently destroy a database someone
    was using. `pause` does not trigger that removal at all, which is the main
    reason it is the default -- but the guard stays on the stop path so choosing
    `stop` cannot reintroduce the hazard.
    """
    action = CONTAINER_ACTION if CONTAINER_ACTION in _CONTAINER_ACTION_REASON else "pause"
    steps: list[Step] = []
    containers = running_containers(run=run)
    if not containers:
        return steps
    steps.append(
        Step(
            1,
            f"{len(containers)} running container(s); action is "
            f"{_CONTAINER_ACTION_REASON[action]}",
            "# no command -- this line explains the container steps that follow",
        )
    )
    for container in containers:
        label = f"{container.name} ({container.runtime}, {container.image})"
        if action == "stop" and container.destructive_to_stop:
            why = (
                "started with --rm, so stopping it DESTROYS it"
                if container.auto_remove
                else "its --rm flag could not be determined, so it is assumed unsafe"
            )
            steps.append(
                Step(
                    1,
                    f"SKIP {label}: {why}",
                    f"# deliberately not run: {container.runtime} stop {container.id}",
                )
            )
            continue
        kind = "container-pause" if action == "pause" else "container-stop"
        target = f"{container.runtime}\t{container.id}\t{container.name}"
        steps.append(
            Step(
                1,
                f"{action} {label}",
                f"{container.runtime} {action} {container.id}",
                Change(kind, target, "running", action),
            )
        )
    return steps


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _service_states(units: Sequence[str]) -> tuple[str, ...]:
    """Return every unit's active state with one systemd round trip.

    `systemctl is-active` accepts multiple units and emits one state per unit in
    argument order.  The quieting plan used to launch one process for every
    named service; on a runner with a slow or absent system bus those fourteen
    identical connection attempts dominated every caller of `plan()`.

    Missing output is treated as unknown in the same safe direction as the old
    per-unit probe: only an explicit ``active`` state creates a stop step.
    """
    if not units:
        return ()
    if shutil.which("systemctl") is None:
        return ("unknown",) * len(units)
    result = subprocess.run(
        ["systemctl", "is-active", *units],
        capture_output=True,
        text=True,
        check=False,
    )
    reported = result.stdout.splitlines()
    return tuple(
        reported[index].strip() or "unknown"
        if index < len(reported)
        else "unknown"
        for index in range(len(units))
    )


# --- the watchdog -----------------------------------------------------------


def watchdog_scope() -> str:
    """`--system` when running as root, `--user` otherwise.

    Tier 1 needs root, and root is normally reached through `sudo`, which is
    exactly the case where `--user` cannot work: root has no user manager of its
    own, and `sudo -E` hands it an `XDG_RUNTIME_DIR` belonging to uid 1000 whose
    bus refuses a connection from another uid. The watchdog therefore refused to
    arm in the one situation it was written for, and `apply()` -- correctly --
    refused to change anything.

    System scope is the stronger guarantee, not a concession: a system transient
    timer outlives the login session as well as the process, so closing the
    laptop lid on a frozen desktop still thaws it. The scope follows the euid
    rather than a flag because the two are the same question -- a non-root caller
    cannot create a system unit without polkit, and a root caller cannot reach
    the calling user's bus.
    """
    return "--system" if os.geteuid() == 0 else "--user"


def arm_watchdog(
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    *,
    journal: Path = JOURNAL,
    run: Any = subprocess.run,
    reason: list[str] | None = None,
) -> str:
    """Schedule an unconditional restore, and return the unit that will do it.

    This runs *before* the first change, and `apply()` refuses to proceed if it
    returns empty. The timer is a transient systemd unit, so it survives this
    process dying by any means -- the failure mode it exists for is the
    benchmark being `SIGKILL`ed with the desktop frozen.

    On failure the return is empty and `reason`, if given, collects systemd's own
    explanation. The caller reports it verbatim: "systemd-run unavailable or
    refused" is a guess, and an operator staring at a refusal needs the sentence
    systemd actually printed.
    """
    if shutil.which("systemd-run") is None:
        if reason is not None:
            reason.append("systemd-run is not installed")
        return ""
    unit = f"wreath-quiet-restore-{os.getpid()}"
    command = [
        "systemd-run",
        watchdog_scope(),
        f"--unit={unit}",
        f"--on-active={deadline_seconds}",
        "--timer-property=AccuracySec=1s",
        "--",
        os.environ.get("WREATH_QUIET_PYTHON", sys.executable),
        "-m",
        "wreath._devtools.quiet",
        "--restore",
        "--journal",
        str(journal),
    ]
    result = run(command, capture_output=True, text=True, check=False)
    if getattr(result, "returncode", 1) != 0:
        if reason is not None:
            output = (getattr(result, "stderr", "") or "").strip()
            reason.append(
                f"`{' '.join(command[:3])} ...` exited "
                f"{getattr(result, 'returncode', '?')}"
                + (f": {output}" if output else "")
            )
        return ""
    return unit


def disarm_watchdog(unit: str, *, run: Any = subprocess.run) -> None:
    """Cancel the timer once the benchmark has restored the machine itself."""
    if not unit or shutil.which("systemctl") is None:
        return
    run(["systemctl", watchdog_scope(), "stop", f"{unit}.timer"], check=False,
        capture_output=True)


def watchdog_armed(unit: str, *, run: Any = subprocess.run) -> bool:
    """Whether the restore timer is really scheduled. Checked, never assumed."""
    if not unit or shutil.which("systemctl") is None:
        return False
    result = run(
        ["systemctl", watchdog_scope(), "is-active", f"{unit}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    return getattr(result, "stdout", "").strip() in {"active", "activating"}


# --- applying ---------------------------------------------------------------


class QuietRefused(RuntimeError):
    """Raised when the machine cannot be quieted safely, so it was not touched."""


def _refuse_unless_journalable(path: Path) -> None:
    """Prove the journal can be written *before* anything is armed or changed.

    A change that cannot be recorded is one nothing can undo, so an unwritable
    journal has to refuse on the same ground the watchdog does. Checking it here
    rather than at the first write is what makes the refusal a sentence instead
    of a `PermissionError` traceback out of `pathlib`, and what keeps the failure
    from happening *after* the watchdog is armed -- five orphaned timers
    accumulated on one machine from repeated failures at exactly that point.
    """
    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a"):
            pass
    except OSError as error:
        hint = ""
        if existed:
            hint = (
                f" A journal from an earlier run is already there and belongs to "
                f"another user; `sudo rm {path}` clears it."
            )
        raise QuietRefused(
            f"cannot write the journal at {path}: {error.strerror}; refusing to "
            f"change anything, because a change that is not recorded is one nothing "
            f"can undo.{hint}"
        ) from error
    if not existed:
        # Leave the tree exactly as it was found. `apply()` promises that a run
        # which refuses has written nothing, and a probe is not an exception to
        # that -- an empty journal on disk reads as a run that quieted nothing and
        # would send `--restore` looking for changes that were never made.
        path.unlink(missing_ok=True)


def apply(
    tier: int,
    *,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    journal_path: Path = JOURNAL,
    pid: int | None = None,
    allow_competing: bool = False,
) -> Journal:
    """Quiet the machine to `tier`, refusing outright if restore is not proven.

    The ordering is the safety property: arm, verify armed, journal, *then*
    change. Any other order has a window where a crash leaves the machine
    quieted with nothing scheduled to undo it.

    Refuses first on a *different* ground: another benchmark, test run or agent
    executing right now. Quieting the machine around a competing workload
    produces a number that looks careful and is not, which is worse than an
    obviously noisy one -- so this refuses at every tier, including tier 0 where
    nothing would have been changed anyway.
    """
    if not allow_competing:
        competing = competing_workloads(pid=pid)
        if competing:
            listed = "\n".join(
                f"    pid {w.pid:>7}  {w.why}\n              {w.command}"
                for w in competing[:12]
            )
            more = f"\n    ... and {len(competing) - 12} more" if len(competing) > 12 else ""
            raise QuietRefused(
                f"{len(competing)} competing workload(s) are running; a benchmark "
                f"taken alongside them measures them too:\n{listed}{more}\n"
                "  Stop them, or pass --allow-competing to measure anyway and "
                "label the result accordingly."
            )
    steps = [step for step in plan(tier, pid=pid) if step.change is not None]
    journal = Journal(deadline=time.time() + deadline_seconds, armed_at=time.time())
    if tier >= 1 and steps:
        _refuse_unless_journalable(journal_path)
        reason: list[str] = []
        unit = arm_watchdog(deadline_seconds, journal=journal_path, reason=reason)
        if not unit:
            detail = f" -- {reason[0]}" if reason else ""
            raise QuietRefused(
                f"could not arm the restore watchdog{detail}; refusing to change "
                "anything, because a change this process cannot guarantee to undo "
                "is not one it should make"
            )
        if not watchdog_armed(unit):
            disarm_watchdog(unit)
            raise QuietRefused(
                f"armed {unit}.timer but systemd does not report it active; "
                "refusing to change anything"
            )
        journal.watchdog = unit
    applied = 0
    try:
        for step in steps:
            change = step.change
            if change is None:  # pragma: no cover - filtered above, guarded not asserted
                continue
            journal.changes.append(change)
            journal.write(journal_path)
            _apply_one(change)
            applied += 1
    except OSError as error:
        # Nothing was changed, so nothing needs undoing -- and a timer left armed
        # over an empty machine is litter that accrues one unit per failed attempt.
        # Past the first change the opposite holds: the watchdog stays, because a
        # half-quieted machine is precisely what it exists to recover.
        if applied == 0:
            disarm_watchdog(journal.watchdog)
            journal_path.unlink(missing_ok=True)
            raise QuietRefused(
                f"could not apply the first change ({error.strerror}); the machine "
                f"is untouched and the watchdog has been disarmed"
            ) from error
        raise
    return journal


def _apply_one(change: Change) -> None:
    if change.kind in {"sysfs", "freeze"}:
        Path(change.target).write_text(change.desired)
    elif change.kind == "service":
        subprocess.run(["systemctl", "stop", change.target], check=False)
    elif change.kind in {"container-pause", "container-stop"}:
        runtime, _, rest = change.target.partition("\t")
        cid = rest.partition("\t")[0]
        verb = "pause" if change.kind == "container-pause" else "stop"
        subprocess.run([runtime, verb, cid], check=False, capture_output=True)


# --- variance measurement ---------------------------------------------------


def measure_noise(samples: int = 7, spin_ms: int = 40) -> dict[str, float]:
    """An A/A spread for the *machine*, not for any benchmark.

    Times the same fixed CPU-bound loop repeatedly and reports the spread. It
    measures nothing about wreath and everything about whether this machine can
    currently produce a number worth reporting, which is the question a tier is
    chosen to answer.
    """
    timings: list[float] = []
    budget = spin_ms / 1000.0
    for _ in range(samples):
        start = time.perf_counter()
        deadline = start + budget
        count = 0
        while time.perf_counter() < deadline:
            count += 1
        timings.append(count / (time.perf_counter() - start))
    best, worst = max(timings), min(timings)
    median = sorted(timings)[len(timings) // 2]
    return {
        "median_ops": median,
        "best_ops": best,
        "worst_ops": worst,
        "spread_pct": (best - worst) / best * 100.0 if best else 0.0,
    }


# --- CLI --------------------------------------------------------------------


def _print_plan(steps: Sequence[Step], tier: int) -> None:
    print(f"\nwreath-bench --quiet={tier} would make {len(steps)} change(s):\n")
    for step in steps:
        marker = "  " if step.change is None else "* "
        print(f"{marker}[tier {step.tier}] {step.description}")
        print(f"      {step.command}")
    privileged = [step for step in steps if step.change is not None]
    if privileged:
        print(
            f"\n{len(privileged)} of these need root. Nothing above has been done. "
            "Re-run with --apply to do it."
        )


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m wreath._devtools.quiet` -- plan, apply, or restore."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="wreath-bench-quiet",
        description="Quiet this machine for benchmarking, reversibly.",
    )
    parser.add_argument("--tier", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--apply", action="store_true",
                        help="actually make the changes (default is a dry run)")
    parser.add_argument("--restore", action="store_true",
                        help="undo everything in the journal and exit")
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    parser.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE_SECONDS,
                        help="seconds before the watchdog restores unconditionally")
    parser.add_argument("--measure-noise", action="store_true",
                        help="report this machine's current A/A spread and exit")
    parser.add_argument("--check-competing", action="store_true",
                        help="list processes that would contaminate a run, and exit")
    parser.add_argument("--allow-competing", action="store_true",
                        help="quiet the machine even though other work is running")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check_competing:
        competing = competing_workloads()
        if not competing:
            print("wreath-bench-quiet: no competing workloads; the machine is idle.")
            return 0
        print(f"wreath-bench-quiet: {len(competing)} competing workload(s):")
        for workload in competing:
            print(f"  pid {workload.pid:>7}  {workload.why}")
            print(f"            {workload.command}")
        return 1

    if args.restore:
        lines = restore(args.journal)
        if not lines:
            print("wreath-bench-quiet: nothing to restore.")
        for line in lines:
            print(f"wreath-bench-quiet: {line}")
        return 0
    if args.measure_noise:
        noise = measure_noise()
        print(f"A/A spread: {noise['spread_pct']:.2f}% "
              f"(median {noise['median_ops']:,.0f} ops/s)")
        return 0
    steps = plan(args.tier)
    if not args.apply:
        _print_plan(steps, args.tier)
        return 0
    try:
        journal = apply(args.tier, deadline_seconds=args.deadline,
                        journal_path=args.journal,
                        allow_competing=args.allow_competing)
    except QuietRefused as error:
        print(f"wreath-bench-quiet: REFUSED -- {error}")
        return 2
    print(f"wreath-bench-quiet: applied {len(journal.changes)} change(s); "
          f"watchdog {journal.watchdog or '(none needed)'} restores in "
          f"{args.deadline}s if nothing else does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
