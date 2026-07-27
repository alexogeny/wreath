"""The safety properties of `wreath-bench --quiet`, pinned.

Every test here exists because the failure it guards against costs the operator
their desktop or their terminal. They are cheap, they run without privileges,
and none of them touches a real session -- the freeze paths are exercised
against synthetic cgroup trees under `tmp_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wreath._devtools import quiet

# --- the ancestry exemption, which is the property that matters most ---------


def test_session_ancestry_walks_to_the_root() -> None:
    """Every ancestor is listed, ending at `/`, so none can be missed."""
    ancestry = quiet.session_ancestry()
    if not ancestry:  # pragma: no cover - cgroup v1 or a non-Linux runner
        pytest.skip("no cgroup v2 path for this process")
    assert ancestry[-1] == "/"
    assert len(ancestry) == len(set(ancestry)), "an ancestor appeared twice"
    for parent, child in zip(ancestry[1:], ancestry[:-1], strict=True):
        assert child.startswith(parent), f"{child} is not under {parent}"


def test_no_freezable_target_is_an_ancestor_of_this_process() -> None:
    """The benchmark can never freeze the shell it was launched from.

    On a GNOME desktop the benchmark runs *inside* the terminal's `app.slice`,
    so "freeze the user session" would include the operator's shell. This is the
    assertion that makes that impossible, and it is the reason `--quiet=2` is
    safe to offer at all.
    """
    exempt = set(quiet.session_ancestry())
    if not exempt:  # pragma: no cover - see above
        pytest.skip("no cgroup v2 path for this process")
    for target in quiet.freezable_targets():
        relative = "/" + str(target.relative_to(quiet._CGROUP_ROOT))
        assert relative not in exempt, f"{relative} is an ancestor of this process"
        for ancestor in exempt:
            assert not ancestor.startswith(relative + "/"), (
                f"freezing {relative} would suspend {ancestor}, which contains us"
            )


#: Deliberately a literal, and deliberately *not* `quiet._NEVER_FREEZE`.
#:
#: The first version of the test below iterated the module's own constant, so
#: emptying that constant emptied the check: the mutation that should have
#: proved the guard works made the test pass instead. A test for a denylist has
#: to own its own copy of what the denylist is for, or it is only asserting that
#: the code agrees with itself.
_MUST_NEVER_FREEZE = (
    "session-manager",
    "keyring",
    "ssh-agent",
    "gpg-agent",
    "dbus",
    "portal",
    "at-spi",
    "dconf",
)


def test_session_infrastructure_is_never_a_freeze_target() -> None:
    """Excluding ancestors is not enough; a frozen D-Bus hangs the terminal too.

    The terminal survives a frozen `dbus.socket` exactly as long as it takes to
    make its next D-Bus call. Same for the ssh-agent and the session manager --
    none of them is an ancestor, and all of them take the desktop with them.
    """
    for target in quiet.freezable_targets():
        lowered = target.name.lower()
        for banned in _MUST_NEVER_FREEZE:
            assert banned not in lowered, f"{target.name} matches banned {banned!r}"
        assert target.name.endswith(".scope"), (
            f"{target.name} is not a transient application scope; services and "
            "sockets are session infrastructure and must not be frozen"
        )


def test_the_implementation_still_covers_everything_the_test_requires() -> None:
    """The two lists may diverge only in the safe direction.

    The implementation may ban *more* than the test demands; it may never ban
    less. Without this, the independent list above silently stops matching what
    the code does.
    """
    missing = set(_MUST_NEVER_FREEZE) - set(quiet._NEVER_FREEZE)
    assert not missing, f"the implementation no longer excludes {sorted(missing)}"


# --- core allocation --------------------------------------------------------


def test_split_cores_never_shares_a_physical_core() -> None:
    """Server and generator must not hold two threads of one core.

    This is the defect the split was written to fix: taking CPUs 0 and 1 on a
    uniform SMT machine gives the server one thread each of two cores and hands
    both siblings to the generator, so the two sides contend inside a core.
    """
    cores = quiet.physical_cores()
    if not cores:  # pragma: no cover - no sysfs topology
        pytest.skip("no CPU topology available")
    split = quiet.split_cores()
    if not split.whole_cores:
        pytest.skip(f"machine too small for a whole-core split: {split.reason}")
    server, client = set(split.server), set(split.client)
    assert not server & client, "a CPU was given to both sides"
    for members in cores.values():
        group = set(members)
        assert not (group & server and group & client), (
            f"core {sorted(group)} is split between server and generator"
        )


# --- the journal and restore ------------------------------------------------


def test_restore_of_a_missing_journal_is_a_clean_no_op(tmp_path: Path) -> None:
    assert quiet.restore(tmp_path / "absent.json") == []


def test_restore_is_idempotent(tmp_path: Path) -> None:
    """Running restore twice must not fail, and must not undo anything twice."""
    node = tmp_path / "knob"
    node.write_text("1")
    journal = tmp_path / "journal.json"
    quiet.Journal(changes=[quiet.Change("sysfs", str(node), "0", "1")]).write(journal)

    first = quiet.restore(journal)
    assert len(first) == 1
    assert node.read_text() == "0"
    assert quiet.restore(journal) == []
    assert node.read_text() == "0"


def test_restore_runs_in_reverse_order(tmp_path: Path) -> None:
    """Later changes are undone first, so a dependent pair unwinds correctly."""
    order: list[str] = []
    for name in ("a", "b"):
        (tmp_path / name).write_text("changed")
    journal = tmp_path / "journal.json"
    quiet.Journal(
        changes=[
            quiet.Change("sysfs", str(tmp_path / "a"), "a0", "changed"),
            quiet.Change("sysfs", str(tmp_path / "b"), "b0", "changed"),
        ]
    ).write(journal)
    for line in quiet.restore(journal):
        order.append(line.split()[2])
    assert order == [str(tmp_path / "b"), str(tmp_path / "a")]


def test_a_freeze_change_restores_the_previous_value(tmp_path: Path) -> None:
    node = tmp_path / "cgroup.freeze"
    node.write_text("1")
    journal = tmp_path / "journal.json"
    quiet.Journal(changes=[quiet.Change("freeze", str(node), "0", "1")]).write(journal)
    quiet.restore(journal)
    assert node.read_text() == "0", "a frozen cgroup was not thawed"


def test_a_stopped_service_is_started_only_if_it_was_active(tmp_path: Path) -> None:
    """A service that was already stopped must not be started by the restore."""
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return type("R", (), {"returncode": 0, "stdout": ""})()

    journal = tmp_path / "journal.json"
    quiet.Journal(
        changes=[
            quiet.Change("service", "was-active.service", "active", "stopped"),
            quiet.Change("service", "was-idle.service", "inactive", "stopped"),
        ]
    ).write(journal)
    quiet.restore(journal, run=fake_run)
    started = [command[2] for command in calls if command[:2] == ["systemctl", "start"]]
    assert started == ["was-active.service"]


def test_the_journal_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    original = quiet.Journal(
        changes=[quiet.Change("sysfs", "/x", "a", "b")],
        armed_at=1.0,
        deadline=2.0,
        watchdog="unit",
    )
    original.write(path)
    restored = quiet.Journal.read(path)
    assert restored.watchdog == "unit"
    assert restored.changes[0].previous == "a"
    assert json.loads(path.read_text())["changes"][0]["kind"] == "sysfs"


# --- refusing to act without a proven restore -------------------------------


def test_apply_refuses_when_the_watchdog_cannot_be_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No restore, no quieting -- and nothing is changed on the way to refusing.

    This is the assertion that makes the whole feature safe to offer: a change
    this process cannot guarantee to undo is one it must not make.
    """
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "")
    journal = tmp_path / "journal.json"
    with pytest.raises(quiet.QuietRefused, match="watchdog"):
        quiet.apply(1, journal_path=journal, allow_competing=True)
    assert not journal.exists(), "a journal was written before the refusal"


def test_apply_refuses_when_the_armed_watchdog_is_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arming is not trusted on its own; systemd is asked whether it took."""
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "phantom-unit")
    monkeypatch.setattr(quiet, "watchdog_armed", lambda *a, **k: False)
    with pytest.raises(quiet.QuietRefused, match="does not report it active"):
        quiet.apply(1, journal_path=tmp_path / "journal.json", allow_competing=True)


def test_tier_zero_needs_no_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 0 changes nothing that needs undoing, so it must not demand one."""
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "")
    journal = quiet.apply(0, allow_competing=True)
    assert journal.changes == []


# --- the plan ---------------------------------------------------------------


def test_the_plan_is_ordered_by_tier() -> None:
    steps = quiet.plan(2)
    tiers = [step.tier for step in steps]
    assert tiers == sorted(tiers), "the plan jumps between tiers"


def test_only_privileged_steps_carry_a_change() -> None:
    """Tier 0 is applied in-process, so it has nothing to journal or undo."""
    for step in quiet.plan(0):
        assert step.change is None


def test_every_privileged_step_records_a_previous_value() -> None:
    """A change with no `previous` cannot be undone, so it must not exist."""
    for step in quiet.plan(2):
        if step.change is not None:
            assert step.change.previous != "", f"{step.description} has no prior value"
            assert step.change.previous != step.change.desired


def test_the_service_list_is_named_not_inferred() -> None:
    """A rule like "stop anything idle" is how a harness takes out a database."""
    assert quiet.NOISY_SERVICES, "the service list is empty"
    for service in quiet.NOISY_SERVICES:
        assert service.endswith((".service", ".timer"))


# --- named applications -----------------------------------------------------


def test_no_named_application_can_match_session_infrastructure() -> None:
    """`HEAVY_APPS` must not be able to reach anything `_NEVER_FREEZE` bans.

    The two lists are matched against the same scope names, so an entry like
    "dbus" in the app list would collide with the denylist and the outcome would
    depend on which check ran first. Keeping them provably disjoint means a
    careless addition to the app list cannot take out the desktop even if the
    ordering is later changed.
    """
    for app in quiet.HEAVY_APPS:
        for banned in _MUST_NEVER_FREEZE:
            assert banned not in app and app not in banned, (
                f"named app {app!r} overlaps banned infrastructure {banned!r}"
            )


def test_named_app_targets_are_a_subset_of_freezable_targets() -> None:
    """Named targeting must inherit every safety filter, not re-derive them.

    `named_app_targets` is a filter over `freezable_targets`, so the ancestry
    exemption and the denylist apply unchanged.

    The subset assertion alone is **not enough**, and finding that out is why
    this test has two halves. A mutation that made `named_app_targets` walk the
    whole cgroup tree itself still satisfied the subset on this machine, purely
    because the paths it found happened to coincide -- a machine with a banned
    scope under a different parent would have been silently unprotected. So the
    delegation itself is asserted, not just its observable result on whatever
    machine the suite happens to run on.
    """
    named = set(quiet.named_app_targets())
    every = set(quiet.freezable_targets())
    assert named <= every, f"named targeting escaped the safety filter: {named - every}"

    source = Path(quiet.__file__).read_text()
    body = source.split("def named_app_targets")[1].split("\ndef ")[0]
    assert "freezable_targets(pid)" in body, (
        "named_app_targets no longer delegates to freezable_targets; the ancestry "
        "exemption and _NEVER_FREEZE denylist are no longer guaranteed to apply"
    )
    assert "_CGROUP_ROOT" not in body, (
        "named_app_targets walks the cgroup tree itself, which is a second place "
        "for the safety filters to be got wrong"
    )


def test_a_named_app_is_frozen_at_tier_one_not_only_at_tier_two() -> None:
    """The measured advice is to stop at tier 1, so tier 1 must cover browsers.

    Before this, `--quiet=1` left a browser running -- the noisiest thing on a
    developer desktop -- because freezing lived only in the tier nobody was
    told to use.
    """
    if not quiet.named_app_targets():  # pragma: no cover - no heavy app running
        pytest.skip("no named application is running on this machine")
    tiers = {step.tier for step in quiet.plan(1) if "freeze application" in step.description}
    assert tiers == {1}, "named applications are not frozen at tier 1"


def test_tier_two_does_not_refreeze_what_tier_one_named() -> None:
    """A cgroup journalled twice would be thawed twice, and read as two changes."""
    targets = [
        step.change.target
        for step in quiet.plan(2)
        if step.change is not None and step.change.kind == "freeze"
    ]
    assert len(targets) == len(set(targets)), "a cgroup appears twice in the plan"


# --- containers -------------------------------------------------------------


def _container(name: str, *, auto: bool = False, unknown: bool = False) -> quiet.Container:
    return quiet.Container("docker", f"id-{name}", name, "img", auto, unknown)


def test_an_auto_remove_container_is_destructive_to_stop() -> None:
    """`--rm` means stopping DELETES it, which is data loss, not a quiet-down."""
    assert _container("rm", auto=True).destructive_to_stop
    assert not _container("keep").destructive_to_stop


def test_an_undeterminable_container_is_assumed_destructive() -> None:
    """When the inspect fails, the safe answer is to leave the container alone.

    Defaulting the other way means one flaky `docker inspect` is enough to
    delete somebody's database.
    """
    assert _container("mystery", unknown=True).destructive_to_stop


def test_the_stop_path_skips_a_container_it_would_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `--rm` guard lives on the stop path and is proved there, not assumed."""
    monkeypatch.setattr(quiet, "CONTAINER_ACTION", "stop")
    monkeypatch.setattr(
        quiet,
        "running_containers",
        lambda **_: (_container("doomed", auto=True), _container("safe")),
    )
    steps = quiet._container_steps()
    doomed = [s for s in steps if "doomed" in s.description]
    safe = [s for s in steps if "safe" in s.description]
    assert doomed and doomed[0].change is None, "a --rm container was scheduled for stop"
    assert "DESTROYS" in doomed[0].description
    assert safe and safe[0].change is not None, "a safe container was not stopped"


def test_the_default_action_never_destroys_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause is the default *because* it makes the `--rm` hazard unreachable."""
    monkeypatch.setattr(quiet, "CONTAINER_ACTION", "pause")
    monkeypatch.setattr(
        quiet, "running_containers", lambda **_: (_container("rm-one", auto=True),)
    )
    changes = [s.change for s in quiet._container_steps() if s.change is not None]
    assert len(changes) == 1, "pause skipped a container it did not need to skip"
    assert changes[0].kind == "container-pause"


def test_a_paused_container_is_unpaused_and_a_stopped_one_started(
    tmp_path: Path,
) -> None:
    """Each container action has its own inverse; using one for both wedges it."""
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return type("R", (), {"returncode": 0, "stdout": ""})()

    journal = tmp_path / "journal.json"
    quiet.Journal(
        changes=[
            quiet.Change("container-pause", "docker\tabc\tpaused-one", "running", "pause"),
            quiet.Change("container-stop", "docker\tdef\tstopped-one", "running", "stop"),
        ]
    ).write(journal)
    quiet.restore(journal, run=fake_run)
    assert ["docker", "start", "def"] in calls
    assert ["docker", "unpause", "abc"] in calls


def test_a_container_change_carries_its_name_for_a_human(tmp_path: Path) -> None:
    """A bare hex id in a journal tells an operator nothing at 3am."""
    monkey = quiet.Change("container-pause", "docker\tdeadbeef\twreath-test-pg", "running",
                          "pause")
    path = tmp_path / "journal.json"
    quiet.Journal(changes=[monkey]).write(path)
    assert "wreath-test-pg" in path.read_text()


# --- competing workloads ----------------------------------------------------


def test_the_competing_check_never_finds_itself() -> None:
    """A check that always fires gets overridden by habit, and then it is not one.

    This process is a Python interpreter running out of this repository, which
    is exactly the pattern the check looks for. Excluding the caller's whole
    ancestry is what stops it reporting the benchmark as its own competitor.
    """
    own = quiet._own_process_tree()
    assert os.getpid() in own, "the checker does not exempt itself"
    for workload in quiet.competing_workloads():
        assert workload.pid not in own, f"pid {workload.pid} is our own ancestry"


def test_an_idle_shell_in_the_repository_is_not_a_competing_workload() -> None:
    """Being *in* the repo is not the same as *working*.

    The first version matched any process whose cwd was the repository, which
    caught every idle shell and pipeline member. Reporting those trains the
    operator to pass `--allow-competing` reflexively.
    """
    assert not quiet._is_workload("/usr/bin/bash")
    assert not quiet._is_workload("/usr/bin/head")
    assert not quiet._is_workload("/usr/bin/less")
    assert quiet._is_workload("/home/alex/private/neo/.venv/bin/python")
    assert quiet._is_workload("/usr/bin/python3.14")
    assert quiet._is_workload("/usr/bin/cc1")


def test_the_competing_check_reads_proc_not_the_command_line() -> None:
    """Association with the repo comes from `exe`/`cwd`, never a cmdline match.

    A process launched as `.venv/bin/python` from the repository root has no
    absolute path in its command line at all, so a substring match reports an
    idle machine while an agent is running -- the exact failure the check exists
    to prevent. This pins the resolution to `/proc`.
    """
    source = Path(quiet.__file__).read_text()
    body = source.split("def competing_workloads")[1].split("\ndef ")[0]
    assert "_proc_link(entry, \"exe\")" in body
    assert "_proc_link(entry, \"cwd\")" in body


def test_apply_refuses_while_another_workload_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement is guarded as well as the machine, at every tier."""
    monkeypatch.setattr(
        quiet,
        "competing_workloads",
        lambda **_: (quiet.Workload(4242, "pytest -x", "a test run"),),
    )
    with pytest.raises(quiet.QuietRefused, match="competing workload"):
        quiet.apply(0, journal_path=tmp_path / "journal.json")


def test_the_competing_refusal_can_be_overridden_deliberately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override must exist, or the check blocks a legitimate measurement."""
    monkeypatch.setattr(
        quiet,
        "competing_workloads",
        lambda **_: (quiet.Workload(4242, "pytest -x", "a test run"),),
    )
    journal = quiet.apply(0, journal_path=tmp_path / "j.json", allow_competing=True)
    assert journal.changes == []
