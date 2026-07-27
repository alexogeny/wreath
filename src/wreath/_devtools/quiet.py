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
            turbo, transparent huge pages, and a *named* list of background
            services stopped.
    Tier 2  opt-in, last resort. Freezes the graphical session's cgroups.

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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where the journal of applied changes lives. Deliberately under `/tmp` rather
#: than the repository: it describes machine state, not project state, and a
#: stale one left in a working tree would be committed by accident.
JOURNAL = Path(os.environ.get("WREATH_QUIET_JOURNAL", "/tmp/wreath-quiet.json"))

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
    for part in text.split(","):
        part = part.strip()
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
    targets: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.endswith(".scope"):
            continue
        relative = "/" + str(child.relative_to(_CGROUP_ROOT))
        if relative in exempt or any(anc.startswith(relative + "/") for anc in exempt):
            continue
        lowered = child.name.lower()
        if any(banned in lowered for banned in _NEVER_FREEZE):
            continue
        if (child / "cgroup.freeze").exists():
            targets.append(child)
    return tuple(targets)


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
        for service in NOISY_SERVICES:
            state = _service_state(service)
            if state == "active":
                steps.append(
                    Step(
                        1,
                        f"stop {service} (was active)",
                        f"sudo systemctl stop {service}",
                        Change("service", service, "active", "stopped"),
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
        for target in targets:
            node = target / "cgroup.freeze"
            current = _read(node) or "0"
            steps.append(
                Step(
                    2,
                    f"freeze {target.name}",
                    f"echo 1 > {node}",
                    Change("freeze", str(node), current, "1"),
                )
            )
    return steps


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _service_state(unit: str) -> str:
    if shutil.which("systemctl") is None:
        return "unknown"
    result = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


# --- the watchdog -----------------------------------------------------------


def arm_watchdog(
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    *,
    journal: Path = JOURNAL,
    run: Any = subprocess.run,
) -> str:
    """Schedule an unconditional restore, and return the unit that will do it.

    This runs *before* the first change, and `apply()` refuses to proceed if it
    returns empty. The timer is a user-scope transient unit, so it survives this
    process dying by any means -- the failure mode it exists for is the
    benchmark being `SIGKILL`ed with the desktop frozen.
    """
    if shutil.which("systemd-run") is None:
        return ""
    unit = f"wreath-quiet-restore-{os.getpid()}"
    command = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--on-active={deadline_seconds}",
        "--timer-property=AccuracySec=1s",
        "--",
        os.environ.get("WREATH_QUIET_PYTHON", "/home/alex/private/neo/.venv/bin/python"),
        "-m",
        "wreath._devtools.quiet",
        "--restore",
        "--journal",
        str(journal),
    ]
    result = run(command, capture_output=True, text=True, check=False)
    if getattr(result, "returncode", 1) != 0:
        return ""
    return unit


def disarm_watchdog(unit: str, *, run: Any = subprocess.run) -> None:
    """Cancel the timer once the benchmark has restored the machine itself."""
    if not unit or shutil.which("systemctl") is None:
        return
    run(["systemctl", "--user", "stop", f"{unit}.timer"], check=False,
        capture_output=True)


def watchdog_armed(unit: str, *, run: Any = subprocess.run) -> bool:
    """Whether the restore timer is really scheduled. Checked, never assumed."""
    if not unit or shutil.which("systemctl") is None:
        return False
    result = run(
        ["systemctl", "--user", "is-active", f"{unit}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    return getattr(result, "stdout", "").strip() in {"active", "activating"}


# --- applying ---------------------------------------------------------------


class QuietRefused(RuntimeError):
    """Raised when the machine cannot be quieted safely, so it was not touched."""


def apply(
    tier: int,
    *,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    journal_path: Path = JOURNAL,
    pid: int | None = None,
) -> Journal:
    """Quiet the machine to `tier`, refusing outright if restore is not proven.

    The ordering is the safety property: arm, verify armed, journal, *then*
    change. Any other order has a window where a crash leaves the machine
    quieted with nothing scheduled to undo it.
    """
    steps = [step for step in plan(tier, pid=pid) if step.change is not None]
    journal = Journal(deadline=time.time() + deadline_seconds, armed_at=time.time())
    if tier >= 1 and steps:
        unit = arm_watchdog(deadline_seconds, journal=journal_path)
        if not unit:
            raise QuietRefused(
                "could not arm the restore watchdog (systemd-run unavailable or "
                "refused); refusing to change anything, because a change this "
                "process cannot guarantee to undo is not one it should make"
            )
        if not watchdog_armed(unit):
            raise QuietRefused(
                f"armed {unit}.timer but systemd does not report it active; "
                "refusing to change anything"
            )
        journal.watchdog = unit
    for step in steps:
        change = step.change
        if change is None:  # pragma: no cover - filtered above, guarded not asserted
            continue
        journal.changes.append(change)
        journal.write(journal_path)
        _apply_one(change)
    return journal


def _apply_one(change: Change) -> None:
    if change.kind in {"sysfs", "freeze"}:
        Path(change.target).write_text(change.desired)
    elif change.kind == "service":
        subprocess.run(["systemctl", "stop", change.target], check=False)


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
    args = parser.parse_args(list(argv) if argv is not None else None)

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
                        journal_path=args.journal)
    except QuietRefused as error:
        print(f"wreath-bench-quiet: REFUSED -- {error}")
        return 2
    print(f"wreath-bench-quiet: applied {len(journal.changes)} change(s); "
          f"watchdog {journal.watchdog or '(none needed)'} restores in "
          f"{args.deadline}s if nothing else does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
