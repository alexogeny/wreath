from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchmarks import wreath_server
from wreath._devtools import quiet


@pytest.fixture(autouse=True)
def _no_real_container_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep safety tests independent of a host daemon or its startup latency.

    Container enumeration has its own injected-run boundary below. None of the
    plan/watchdog contracts depend on whether this machine happens to have a
    running Podman socket, so reaching the real daemon here is both nondeterministic
    and several seconds slower on a cold CI runner.
    """
    monkeypatch.setattr(quiet, "container_runtimes", lambda: ())


def test_session_ancestry_walks_to_the_root() -> None:
    ancestry = quiet.session_ancestry()
    if not ancestry:  # pragma: no cover - cgroup v1 or a non-Linux runner
        pytest.skip("no cgroup v2 path for this process")
    assert ancestry[-1] == "/"
    assert len(ancestry) == len(set(ancestry)), "an ancestor appeared twice"
    for parent, child in zip(ancestry[1:], ancestry[:-1], strict=True):
        assert child.startswith(parent), f"{child} is not under {parent}"


def test_no_freezable_target_is_an_ancestor_of_this_process() -> None:
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
    for target in quiet.freezable_targets():
        lowered = target.name.lower()
        for banned in _MUST_NEVER_FREEZE:
            assert banned not in lowered, f"{target.name} matches banned {banned!r}"
        assert target.name.endswith(".scope"), (
            f"{target.name} is not a transient application scope; services and "
            "sockets are session infrastructure and must not be frozen"
        )


def test_only_scopes_with_a_freeze_control_are_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_slice = tmp_path / "user.slice" / "app.slice"
    eligible = app_slice / "app-browser.scope"
    ineligible = app_slice / "app-editor.scope"
    eligible.mkdir(parents=True)
    ineligible.mkdir()
    (eligible / "cgroup.freeze").write_text("0", encoding="utf-8")
    monkeypatch.setattr(quiet, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(
        quiet,
        "session_ancestry",
        lambda _pid=None: ("/user.slice/app.slice", "/user.slice", "/"),
    )

    assert quiet.freezable_targets() == (eligible,)


def test_the_implementation_still_covers_everything_the_test_requires() -> None:
    missing = set(_MUST_NEVER_FREEZE) - set(quiet._NEVER_FREEZE)
    assert not missing, f"the implementation no longer excludes {sorted(missing)}"


def test_split_cores_never_shares_a_physical_core() -> None:
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


def test_benchmark_workers_use_distinct_physical_cores_before_smt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wreath_server,
        "physical_cores",
        lambda: {0: [0, 1], 2: [2, 3], 4: [4, 5]},
    )

    available = {0, 1, 2, 3}
    assert wreath_server._worker_cpu(0, available) == 0
    assert wreath_server._worker_cpu(1, available) == 2
    assert wreath_server._worker_cpu(2, available) == 0


def test_restore_of_a_missing_journal_is_a_clean_no_op(tmp_path: Path) -> None:
    assert quiet.restore(tmp_path / "absent.json") == []


def test_restore_is_idempotent(tmp_path: Path) -> None:
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


def test_apply_refuses_when_the_watchdog_cannot_be_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "")
    journal = tmp_path / "journal.json"
    with pytest.raises(quiet.QuietRefused, match="watchdog"):
        quiet.apply(1, journal_path=journal, allow_competing=True)
    assert not journal.exists(), "a journal was written before the refusal"


def test_the_watchdog_uses_system_scope_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return type("R", (), {"returncode": 0, "stdout": "active", "stderr": ""})()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert quiet.watchdog_scope() == "--system"
    unit = quiet.arm_watchdog(60, run=fake_run)
    assert unit, "arming as root produced no unit"
    assert calls[0][1] == "--system"
    assert "--user" not in calls[0]

    quiet.disarm_watchdog(unit, run=fake_run)
    assert quiet.watchdog_armed(unit, run=fake_run)
    for command in calls[1:]:
        assert command[1] == "--system", f"scope split across calls: {command}"


def test_the_watchdog_uses_user_scope_unprivileged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert quiet.watchdog_scope() == "--user"


def test_a_failed_arming_reports_what_systemd_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def fake_run(_command: list[str], **_kwargs: object) -> object:
        return type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "Failed to connect to bus"}
        )()

    reason: list[str] = []
    assert quiet.arm_watchdog(60, run=fake_run, reason=reason) == ""
    assert "Failed to connect to bus" in reason[0]

    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "")
    with pytest.raises(quiet.QuietRefused, match="could not arm the restore watchdog"):
        quiet.apply(1, journal_path=tmp_path / "journal.json", allow_competing=True)


def test_the_journal_avoids_tmp_when_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WREATH_QUIET_JOURNAL", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert quiet._default_journal() == Path("/run/wreath-quiet.json")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert quiet._default_journal() == Path("/tmp/wreath-quiet.json")
    monkeypatch.setenv("WREATH_QUIET_JOURNAL", "/somewhere/else.json")
    assert quiet._default_journal() == Path("/somewhere/else.json")


def test_an_unwritable_journal_refuses_before_arming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    armed: list[object] = []
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: armed.append(a) or "unit")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")

    with pytest.raises(quiet.QuietRefused, match="cannot write the journal"):
        quiet.apply(1, journal_path=blocker / "journal.json", allow_competing=True)
    assert not armed, "a watchdog was armed for a run that could not be journalled"


def test_a_failed_first_change_disarms_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = quiet.Step(
        tier=1,
        description="synthetic",
        command="true",
        change=quiet.Change(
            kind="sysfs", target=str(tmp_path / "knob"), previous="powersave", desired="performance"
        ),
    )
    disarmed: list[str] = []
    monkeypatch.setattr(quiet, "plan", lambda *a, **k: [step])
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "unit")
    monkeypatch.setattr(quiet, "watchdog_armed", lambda *a, **k: True)
    monkeypatch.setattr(quiet, "disarm_watchdog", lambda unit, **k: disarmed.append(unit))
    monkeypatch.setattr(
        quiet,
        "_apply_one",
        lambda _change: (_ for _ in ()).throw(PermissionError(13, "Permission denied")),
    )
    journal = tmp_path / "journal.json"

    with pytest.raises(quiet.QuietRefused, match="machine is untouched"):
        quiet.apply(1, journal_path=journal, allow_competing=True)
    assert disarmed == ["unit"], "the watchdog outlived a run that changed nothing"
    assert not journal.exists(), "a journal survived a run that changed nothing"


def test_apply_refuses_when_the_armed_watchdog_is_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "phantom-unit")
    monkeypatch.setattr(quiet, "watchdog_armed", lambda *a, **k: False)
    with pytest.raises(quiet.QuietRefused, match="does not report it active"):
        quiet.apply(1, journal_path=tmp_path / "journal.json", allow_competing=True)


def test_tier_zero_needs_no_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *a, **k: "")
    journal = quiet.apply(0, allow_competing=True)
    assert journal.changes == []


def test_the_plan_is_ordered_by_tier() -> None:
    steps = quiet.plan(2)
    tiers = [step.tier for step in steps]
    assert tiers == sorted(tiers), "the plan jumps between tiers"


def test_service_states_are_collected_in_one_systemd_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> object:
        calls.append(command)
        return type(
            "R",
            (),
            {"stdout": "active\ninactive\n", "returncode": 3},
        )()

    monkeypatch.setattr(quiet.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(quiet.subprocess, "run", fake_run)

    assert quiet._service_states(("one.service", "two.service")) == (
        "active",
        "inactive",
    )
    assert calls == [
        ["systemctl", "is-active", "one.service", "two.service"],
    ]


def test_service_states_fail_closed_when_systemd_omits_a_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = type("R", (), {"stdout": "active\n", "returncode": 3})()
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(quiet.subprocess, "run", lambda *_args, **_kwargs: result)

    assert quiet._service_states(("one.service", "missing.service")) == (
        "active",
        "unknown",
    )


def test_only_privileged_steps_carry_a_change() -> None:
    for step in quiet.plan(0):
        assert step.change is None


def test_every_privileged_step_records_a_previous_value() -> None:
    for step in quiet.plan(2):
        if step.change is not None:
            assert step.change.previous != "", f"{step.description} has no prior value"
            assert step.change.previous != step.change.desired


def test_the_service_list_is_named_not_inferred() -> None:
    assert quiet.NOISY_SERVICES, "the service list is empty"
    for service in quiet.NOISY_SERVICES:
        assert service.endswith((".service", ".timer"))


def test_no_named_application_can_match_session_infrastructure() -> None:
    for app in quiet.HEAVY_APPS:
        for banned in _MUST_NEVER_FREEZE:
            assert banned not in app and app not in banned, (
                f"named app {app!r} overlaps banned infrastructure {banned!r}"
            )


def test_named_app_targets_are_a_subset_of_freezable_targets() -> None:
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
    if not quiet.named_app_targets():  # pragma: no cover - no heavy app running
        pytest.skip("no named application is running on this machine")
    tiers = {step.tier for step in quiet.plan(1) if "freeze application" in step.description}
    assert tiers == {1}, "named applications are not frozen at tier 1"


def test_tier_two_does_not_refreeze_what_tier_one_named() -> None:
    targets = [
        step.change.target
        for step in quiet.plan(2)
        if step.change is not None and step.change.kind == "freeze"
    ]
    assert len(targets) == len(set(targets)), "a cgroup appears twice in the plan"


def _container(name: str, *, auto: bool = False, unknown: bool = False) -> quiet.Container:
    return quiet.Container("docker", f"id-{name}", name, "img", auto, unknown)


def test_an_auto_remove_container_is_destructive_to_stop() -> None:
    assert _container("rm", auto=True).destructive_to_stop
    assert not _container("keep").destructive_to_stop


def test_an_undeterminable_container_is_assumed_destructive() -> None:
    assert _container("mystery", unknown=True).destructive_to_stop


def test_the_stop_path_skips_a_container_it_would_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(quiet, "CONTAINER_ACTION", "pause")
    monkeypatch.setattr(quiet, "running_containers", lambda **_: (_container("rm-one", auto=True),))
    changes = [s.change for s in quiet._container_steps() if s.change is not None]
    assert len(changes) == 1, "pause skipped a container it did not need to skip"
    assert changes[0].kind == "container-pause"


def test_a_paused_container_is_unpaused_and_a_stopped_one_started(
    tmp_path: Path,
) -> None:
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
    monkey = quiet.Change("container-pause", "docker\tdeadbeef\twreath-test-pg", "running", "pause")
    path = tmp_path / "journal.json"
    quiet.Journal(changes=[monkey]).write(path)
    assert "wreath-test-pg" in path.read_text()


def test_the_competing_check_never_finds_itself() -> None:
    own = quiet._own_process_tree()
    assert os.getpid() in own, "the checker does not exempt itself"
    for workload in quiet.competing_workloads():
        assert workload.pid not in own, f"pid {workload.pid} is our own ancestry"


def test_an_idle_shell_in_the_repository_is_not_a_competing_workload() -> None:
    assert not quiet._is_workload("/usr/bin/bash")
    assert not quiet._is_workload("/usr/bin/head")
    assert not quiet._is_workload("/usr/bin/less")
    assert quiet._is_workload("/home/alex/private/neo/.venv/bin/python")
    assert quiet._is_workload("/usr/bin/python3.14")
    assert quiet._is_workload("/usr/bin/cc1")


def test_the_competing_check_reads_proc_not_the_command_line() -> None:
    source = Path(quiet.__file__).read_text()
    body = source.split("def competing_workloads")[1].split("\ndef ")[0]
    assert '_proc_link(entry, "exe")' in body
    assert '_proc_link(entry, "cwd")' in body


def test_apply_refuses_while_another_workload_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(
        quiet,
        "competing_workloads",
        lambda **_: (quiet.Workload(4242, "pytest -x", "a test run"),),
    )
    journal = quiet.apply(0, journal_path=tmp_path / "j.json", allow_competing=True)
    assert journal.changes == []
