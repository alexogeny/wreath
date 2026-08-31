from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wreath._devtools import quiet


def _result(
    *, returncode: int = 0, stdout: str = "", stderr: str | None = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _CgroupFile:
    def __init__(self, raw: str | OSError, paths: list[str]) -> None:
        self.raw = raw
        self.paths = paths

    def read_text(self) -> str:
        if isinstance(self.raw, OSError):
            raise self.raw
        return self.raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0::/user.slice/app.slice/app-terminal.scope\n", (
            "/user.slice/app.slice/app-terminal.scope",
            "/user.slice/app.slice",
            "/user.slice",
            "/",
        )),
        ("9:cpu:/legacy\n0::/only\n", ("/only", "/")),
        ("9:cpu:/legacy\nmalformed\n", ()),
        ("0\n", ()),
        ("1::/not-unified\n", ()),
        ("0:controller:/not-unified\n", ()),
        (OSError("gone"), ()),
    ],
)
def test_session_ancestry_parses_only_the_unified_hierarchy(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | OSError,
    expected: tuple[str, ...],
) -> None:
    paths: list[str] = []
    monkeypatch.setattr(quiet, "Path", lambda value: _CgroupFile(raw, paths + [str(value)]))

    assert quiet.session_ancestry(42) == expected


def test_session_ancestry_uses_the_requested_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def fake_path(value: str) -> _CgroupFile:
        paths.append(value)
        return _CgroupFile("0::/\n", paths)

    monkeypatch.setattr(quiet, "Path", fake_path)

    assert quiet.session_ancestry() == ("/",)
    assert quiet.session_ancestry(731) == ("/",)
    assert paths == ["/proc/self/cgroup", "/proc/731/cgroup"]


def test_freezable_targets_refuses_without_a_safe_application_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quiet, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(quiet, "session_ancestry", lambda _pid=None: ())
    assert quiet.freezable_targets() == ()

    monkeypatch.setattr(quiet, "session_ancestry", lambda _pid=None: ("/user.slice", "/"))
    assert quiet.freezable_targets() == ()

    monkeypatch.setattr(
        quiet,
        "session_ancestry",
        lambda _pid=None: ("/user.slice/app.slice", "/user.slice", "/"),
    )
    assert quiet.freezable_targets() == ()


def test_freezable_targets_enforces_every_safety_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_slice = tmp_path / "user.slice" / "app.slice"
    eligible = app_slice / "app-browser.scope"
    protected = app_slice / "app-terminal.scope"
    banned = app_slice / "app-dbus.scope"
    no_control = app_slice / "app-editor.scope"
    wrong_kind = app_slice / "background.service"
    ordinary_file = app_slice / "app-file.scope"
    for target in (eligible, protected, banned, no_control, wrong_kind):
        target.mkdir(parents=True, exist_ok=True)
    for target in (eligible, protected, banned, wrong_kind):
        (target / "cgroup.freeze").write_text("0")
    ordinary_file.write_text("not a directory")
    monkeypatch.setattr(quiet, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(
        quiet,
        "session_ancestry",
        lambda _pid=None: (
            "/user.slice/app.slice/app-terminal.scope/vte.scope",
            "/user.slice/app.slice/app-terminal.scope",
            "/user.slice/app.slice",
            "/user.slice",
            "/",
        ),
    )

    assert quiet.freezable_targets(731) == (eligible,)

    monkeypatch.setattr(
        quiet,
        "session_ancestry",
        lambda _pid=None: (
            "app-browser.scope/child.scope",
            "/user.slice/app.slice",
            "/user.slice",
            "/",
        ),
    )
    assert quiet.freezable_targets(731) == (eligible, protected)


def test_restore_one_covers_every_reversible_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = tmp_path / "frozen"
    frozen.write_text("1")
    absent = tmp_path / "absent"
    governor_a = tmp_path / "cpu0" / "cpufreq" / "scaling_governor"
    governor_b = tmp_path / "cpu1" / "cpufreq" / "scaling_governor"
    for governor in (governor_a, governor_b):
        governor.parent.mkdir(parents=True)
        governor.write_text("performance")
    monkeypatch.setattr(quiet, "_CPU_ROOT", tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> None:
        calls.append(command)

    quiet._restore_one(quiet.Change("freeze", str(frozen), "0", "1"), run)
    quiet._restore_one(quiet.Change("freeze", str(absent), "0", "1"), run)
    quiet._restore_one(quiet.Change("service", "active.service", "active", "stopped"), run)
    quiet._restore_one(quiet.Change("service", "idle.service", "inactive", "stopped"), run)
    quiet._restore_one(quiet.Change("governor", "all", "powersave", "performance"), run)
    quiet._restore_one(
        quiet.Change("container-pause", "podman\tabc\tdatabase", "running", "pause"), run
    )
    quiet._restore_one(
        quiet.Change("container-stop", "docker\tdef\tworker", "running", "stop"), run
    )
    quiet._restore_one(quiet.Change("unknown", "target", "before", "after"), run)

    assert frozen.read_text() == "0"
    assert not absent.exists()
    assert governor_a.read_text() == governor_b.read_text() == "powersave"
    assert calls == [
        ["systemctl", "start", "active.service"],
        ["podman", "unpause", "abc"],
        ["docker", "start", "def"],
    ]


def _isolate_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(quiet, "_CPU_ROOT", tmp_path)
    monkeypatch.setattr(
        quiet,
        "split_cores",
        lambda: quiet.CoreSplit((0,), (2,), True, "one core per side"),
    )
    monkeypatch.setattr(quiet, "_service_states", lambda units: ("inactive",) * len(units))
    monkeypatch.setattr(quiet, "_container_steps", lambda: [])
    monkeypatch.setattr(quiet, "named_app_targets", lambda _pid=None: ())
    monkeypatch.setattr(quiet, "freezable_targets", lambda _pid=None: ())
    monkeypatch.setattr(quiet, "session_ancestry", lambda _pid=None: ())


def test_plan_tier_boundaries_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_plan(monkeypatch, tmp_path)

    assert quiet.plan(-1) == []
    tier_zero = quiet.plan(0)
    assert [step.description for step in tier_zero] == [
        "pin server to CPUs [0], generator to [2] (one core per side)",
        "disable ASLR for benchmark children",
        "renice the benchmark tree to -5",
    ]
    assert all(step.change is None for step in tier_zero)


def test_tier_one_plan_changes_only_non_target_machine_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_plan(monkeypatch, tmp_path)
    governors = [
        tmp_path / "cpu0" / "cpufreq" / "scaling_governor",
        tmp_path / "cpu1" / "cpufreq" / "scaling_governor",
        tmp_path / "cpu2" / "cpufreq" / "scaling_governor",
    ]
    values = {
        str(governors[0]): "powersave",
        str(governors[1]): "performance",
        str(governors[2]): "",
        str(tmp_path / "cpufreq" / "boost"): "1",
        "/sys/kernel/mm/transparent_hugepage/enabled": "[always] madvise never",
        "/proc/sys/kernel/perf_event_paranoid": "2",
    }
    for governor in governors:
        governor.parent.mkdir(parents=True)
        governor.write_text("ignored")
    monkeypatch.setattr(quiet, "_read", lambda path: values.get(str(path), ""))
    monkeypatch.setattr(
        quiet,
        "_service_states",
        lambda units: tuple(
            "active" if index == 0 else "inactive" for index, _ in enumerate(units)
        ),
    )
    container = quiet.Step(
        1,
        "pause database",
        "podman pause abc",
        quiet.Change("container-pause", "podman\tabc\tdatabase", "running", "pause"),
    )
    monkeypatch.setattr(quiet, "_container_steps", lambda: [container])

    changes = [step.change for step in quiet.plan(1) if step.change is not None]

    assert [(change.kind, change.previous, change.desired) for change in changes] == [
        ("sysfs", "powersave", "performance"),
        ("sysfs", "1", "0"),
        ("sysfs", "always", "madvise"),
        ("sysfs", "2", "-1"),
        ("service", "active", "stopped"),
        ("container-pause", "running", "pause"),
    ]
    assert changes[4].target == quiet.NOISY_SERVICES[0]


def test_tier_one_plan_freezes_named_apps_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_plan(monkeypatch, tmp_path)
    named = tmp_path / "app-firefox.scope"
    named.mkdir()
    (named / "cgroup.freeze").write_text("0")
    monkeypatch.setattr(quiet, "named_app_targets", lambda _pid=None: (named,))

    tier_zero = quiet.plan(0)
    tier_one = quiet.plan(1, pid=731)

    assert not any(step.change for step in tier_zero)
    freeze = [step for step in tier_one if step.change and step.change.kind == "freeze"]
    assert len(freeze) == 1
    assert freeze[0].change == quiet.Change("freeze", str(named / "cgroup.freeze"), "0", "1")


@pytest.mark.parametrize(
    ("values", "expected_targets"),
    [
        ({}, set()),
        ({"governor": "performance", "boost": "0", "thp": "madvise", "paranoid": "-1"}, set()),
    ],
)
def test_tier_one_plan_does_not_change_absent_or_already_target_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    expected_targets: set[str],
) -> None:
    _isolate_plan(monkeypatch, tmp_path)
    governor = tmp_path / "cpu0" / "cpufreq" / "scaling_governor"
    governor.parent.mkdir(parents=True)
    governor.write_text("ignored")

    def read(path: Path) -> str:
        text = str(path)
        if text == str(governor):
            return values.get("governor", "")
        if text.endswith("/boost"):
            return values.get("boost", "")
        if text.endswith("/transparent_hugepage/enabled"):
            return values.get("thp", "")
        if text.endswith("/perf_event_paranoid"):
            return values.get("paranoid", "")
        return ""

    monkeypatch.setattr(quiet, "_read", read)
    actual = {
        change.target for step in quiet.plan(1) if (change := step.change) is not None
    }
    assert actual == expected_targets


def test_tier_one_plan_preserves_an_already_unrestricted_perf_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_plan(monkeypatch, tmp_path)
    monkeypatch.setattr(
        quiet,
        "_read",
        lambda path: "-1" if str(path).endswith("/perf_event_paranoid") else "",
    )

    targets = {
        change.target
        for step in quiet.plan(1)
        if (change := step.change) is not None
    }
    assert "/proc/sys/kernel/perf_event_paranoid" not in targets


@pytest.mark.parametrize(
    ("ancestry", "label"),
    [
        ((), "(none)"),
        (("/user.slice/app.slice/app-terminal.scope", "/"), "app-terminal.scope"),
    ],
)
def test_tier_two_plan_reports_exemption_and_avoids_duplicate_freezes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestry: tuple[str, ...],
    label: str,
) -> None:
    _isolate_plan(monkeypatch, tmp_path)
    named = tmp_path / "app-firefox.scope"
    unnamed = tmp_path / "app-editor.scope"
    for target in (named, unnamed):
        target.mkdir()
        (target / "cgroup.freeze").write_text("0")
    monkeypatch.setattr(quiet, "named_app_targets", lambda _pid=None: (named,))
    monkeypatch.setattr(quiet, "freezable_targets", lambda _pid=None: (named, unnamed))
    monkeypatch.setattr(quiet, "session_ancestry", lambda _pid=None: ancestry)

    steps = quiet.plan(2, pid=731)
    tier_two = [step for step in steps if step.tier == 2]
    freezes = [step.change for step in tier_two if step.change is not None]

    assert label in tier_two[0].description
    assert freezes == [quiet.Change("freeze", str(unnamed / "cgroup.freeze"), "0", "1")]


def test_tier_two_plan_records_empty_and_nonzero_freeze_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_plan(monkeypatch, tmp_path)
    empty = tmp_path / "app-empty.scope"
    frozen = tmp_path / "app-frozen.scope"
    for target, state in ((empty, ""), (frozen, "1")):
        target.mkdir()
        (target / "cgroup.freeze").write_text(state)
    monkeypatch.setattr(quiet, "freezable_targets", lambda _pid=None: (empty, frozen))

    changes = [
        step.change
        for step in quiet.plan(2)
        if step.change is not None and step.change.kind == "freeze"
    ]
    assert [change.previous for change in changes] == ["0", "1"]


def test_arm_watchdog_reports_missing_systemd_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: None)
    reason: list[str] = []

    assert quiet.arm_watchdog(reason=reason) == ""
    assert reason == ["systemd-run is not installed"]
    assert quiet.arm_watchdog() == ""


def test_arm_watchdog_missing_binary_needs_no_reason_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: None)
    assert quiet.arm_watchdog(reason=None) == ""


@pytest.mark.parametrize(("stderr", "detail"), [("bus unavailable", ": bus unavailable"), ("", "")])
def test_arm_watchdog_reports_the_failed_command(
    monkeypatch: pytest.MonkeyPatch, stderr: str, detail: str
) -> None:
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    reason: list[str] = []

    assert (
        quiet.arm_watchdog(
            run=lambda *_args, **_kwargs: _result(returncode=17, stderr=stderr),
            reason=reason,
        )
        == ""
    )
    assert "exited 17" in reason[0]
    expected = (
        f"`systemd-run {quiet.watchdog_scope()} "
        f"--unit=wreath-quiet-restore-{quiet.os.getpid()} ...` exited 17{detail}"
    )
    assert reason[0] == expected


def test_arm_watchdog_tolerates_a_missing_error_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    reason: list[str] = []
    assert (
        quiet.arm_watchdog(
            run=lambda *_args, **_kwargs: _result(returncode=1, stderr=None),
            reason=reason,
        )
        == ""
    )
    assert reason[0].endswith("exited 1")


def test_arm_watchdog_returns_the_unit_after_a_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setattr(quiet.os, "getpid", lambda: 731)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return _result()

    assert (
        quiet.arm_watchdog(45, journal=tmp_path / "journal.json", run=run)
        == "wreath-quiet-restore-731"
    )
    assert "--on-active=45" in commands[0]
    assert str(tmp_path / "journal.json") == commands[0][-1]


@pytest.mark.parametrize(("unit", "available"), [("", True), ("unit", False)])
def test_disarm_watchdog_is_a_no_op_without_a_usable_unit(
    monkeypatch: pytest.MonkeyPatch, unit: str, available: bool
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        quiet.shutil,
        "which",
        lambda _name: "/bin/systemctl" if available else None,
    )
    quiet.disarm_watchdog(unit, run=lambda command, **_kwargs: calls.append(command))
    assert calls == []


def test_disarm_watchdog_stops_the_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(quiet.shutil, "which", lambda _name: "/bin/systemctl")
    monkeypatch.setattr(quiet, "watchdog_scope", lambda: "--user")
    quiet.disarm_watchdog("unit", run=lambda command, **_kwargs: calls.append(command))
    assert calls == [["systemctl", "--user", "stop", "unit.timer"]]


@pytest.mark.parametrize(
    ("unit", "available", "state", "expected"),
    [
        ("", True, "active", False),
        ("unit", False, "active", False),
        ("unit", True, "active\n", True),
        ("unit", True, "activating", True),
        ("unit", True, "inactive", False),
    ],
)
def test_watchdog_armed_requires_an_active_systemd_timer(
    monkeypatch: pytest.MonkeyPatch,
    unit: str,
    available: bool,
    state: str,
    expected: bool,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        quiet.shutil,
        "which",
        lambda _name: "/bin/systemctl" if available else None,
    )

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return _result(stdout=state)

    assert quiet.watchdog_armed(unit, run=run) is expected
    assert bool(calls) is (bool(unit) and available)


def test_journal_probe_preserves_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    path.write_text("existing")
    quiet._refuse_unless_journalable(path)
    assert path.read_text() == "existing"


def test_journal_probe_removes_a_new_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "journal.json"
    quiet._refuse_unless_journalable(path)
    assert path.parent.is_dir()
    assert not path.exists()


def test_journal_refusal_mentions_how_to_clear_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.json"
    path.write_text("existing")

    def refuse_open(self: Path, *_args: object, **_kwargs: object) -> None:
        if self == path:
            raise PermissionError(13, "Permission denied")
        return None

    monkeypatch.setattr(Path, "open", refuse_open)
    with pytest.raises(quiet.QuietRefused, match=f"sudo rm {path}"):
        quiet._refuse_unless_journalable(path)


def test_journal_refusal_does_not_offer_to_delete_a_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.json"

    def refuse_open(self: Path, *_args: object, **_kwargs: object) -> None:
        if self == path:
            raise PermissionError(13, "Permission denied")
        return None

    monkeypatch.setattr(Path, "open", refuse_open)
    with pytest.raises(quiet.QuietRefused) as caught:
        quiet._refuse_unless_journalable(path)
    assert "sudo rm" not in str(caught.value)


def test_apply_filters_non_changes_before_arming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        quiet,
        "plan",
        lambda *_args, **_kwargs: [quiet.Step(1, "information", "# none")],
    )
    armed: list[int] = []
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *_args, **_kwargs: armed.append(1) or "unit")

    journal = quiet.apply(1, journal_path=tmp_path / "journal.json", allow_competing=True)

    assert journal.changes == []
    assert armed == []


def test_apply_tier_zero_never_arms_even_with_a_synthetic_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = quiet.Change("sysfs", str(tmp_path / "knob"), "0", "1")
    monkeypatch.setattr(
        quiet,
        "plan",
        lambda *_args, **_kwargs: [quiet.Step(0, "synthetic", "write", change)],
    )
    monkeypatch.setattr(quiet, "competing_workloads", lambda **_kwargs: ())
    armed: list[int] = []
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *_args, **_kwargs: armed.append(1) or "unit")
    applied: list[quiet.Change] = []
    monkeypatch.setattr(quiet, "_apply_one", applied.append)

    journal = quiet.apply(0, journal_path=tmp_path / "journal.json")

    assert armed == []
    assert applied == [change]
    assert journal.changes == [change]


def test_apply_with_no_competition_reaches_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quiet, "competing_workloads", lambda **_kwargs: ())
    planned: list[int] = []
    monkeypatch.setattr(
        quiet,
        "plan",
        lambda tier, **_kwargs: planned.append(tier) or [],
    )

    quiet.apply(0, journal_path=tmp_path / "journal.json")

    assert planned == [0]


@pytest.mark.parametrize(
    ("supplied_reason", "detail"),
    [([], ""), (["bus unavailable"], " -- bus unavailable")],
)
def test_apply_watchdog_refusal_includes_available_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supplied_reason: list[str],
    detail: str,
) -> None:
    change = quiet.Change("sysfs", str(tmp_path / "knob"), "0", "1")
    monkeypatch.setattr(
        quiet,
        "plan",
        lambda *_args, **_kwargs: [quiet.Step(1, "synthetic", "write", change)],
    )
    monkeypatch.setattr(quiet, "_refuse_unless_journalable", lambda _path: None)

    def arm(*_args: object, reason: list[str], **_kwargs: object) -> str:
        reason.extend(supplied_reason)
        return ""

    monkeypatch.setattr(quiet, "arm_watchdog", arm)

    with pytest.raises(quiet.QuietRefused) as caught:
        quiet.apply(1, journal_path=tmp_path / "journal.json", allow_competing=True)
    assert f"restore watchdog{detail};" in str(caught.value)


def test_apply_keeps_the_watchdog_after_a_later_change_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = quiet.Change("sysfs", str(tmp_path / "first"), "a", "b")
    second = quiet.Change("sysfs", str(tmp_path / "second"), "c", "d")
    monkeypatch.setattr(
        quiet,
        "plan",
        lambda *_args, **_kwargs: [
            quiet.Step(1, "first", "first", first),
            quiet.Step(1, "second", "second", second),
        ],
    )
    monkeypatch.setattr(quiet, "_refuse_unless_journalable", lambda _path: None)
    monkeypatch.setattr(quiet, "arm_watchdog", lambda *_args, **_kwargs: "unit")
    monkeypatch.setattr(quiet, "watchdog_armed", lambda _unit: True)
    disarmed: list[str] = []
    monkeypatch.setattr(quiet, "disarm_watchdog", lambda unit: disarmed.append(unit))
    applied: list[quiet.Change] = []

    def apply_one(change: quiet.Change) -> None:
        if applied:
            raise OSError(5, "Input/output error")
        applied.append(change)

    monkeypatch.setattr(quiet, "_apply_one", apply_one)
    journal_path = tmp_path / "journal.json"

    with pytest.raises(OSError, match="Input/output error"):
        quiet.apply(1, journal_path=journal_path, allow_competing=True)

    assert disarmed == []
    saved = quiet.Journal.read(journal_path)
    assert saved.watchdog == "unit"
    assert saved.changes == [first, second]


@pytest.mark.parametrize("count", [1, 13])
def test_apply_refusal_lists_competing_workloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    workloads = tuple(
        quiet.Workload(index, f"command-{index}", f"reason-{index}") for index in range(count)
    )
    monkeypatch.setattr(quiet, "competing_workloads", lambda **_kwargs: workloads)

    with pytest.raises(quiet.QuietRefused) as caught:
        quiet.apply(0, journal_path=tmp_path / "journal.json")

    message = str(caught.value)
    assert f"{count} competing workload(s)" in message
    assert "command-0" in message
    if count == 13:
        assert "... and 1 more" in message
    else:
        assert "... and" not in message


def test_main_check_competing_reports_idle_and_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(quiet, "competing_workloads", lambda: ())
    assert quiet.main(["--check-competing"]) == 0
    assert "machine is idle" in capsys.readouterr().out

    workload = quiet.Workload(731, "uv run wreath test", "a test run")
    monkeypatch.setattr(quiet, "competing_workloads", lambda: (workload,))
    assert quiet.main(["--check-competing"]) == 1
    output = capsys.readouterr().out
    assert "1 competing workload(s)" in output
    assert "pid     731  a test run" in output
    assert "uv run wreath test" in output


def test_main_restore_reports_empty_and_restored_journals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(quiet, "restore", lambda _path: [])
    assert quiet.main(["--restore", "--journal", "/journal"]) == 0
    assert "nothing to restore" in capsys.readouterr().out

    monkeypatch.setattr(quiet, "restore", lambda _path: ["restored sysfs /knob -> 0"])
    assert quiet.main(["--restore", "--journal", "/journal"]) == 0
    assert "restored sysfs /knob -> 0" in capsys.readouterr().out


def test_main_covers_measure_plan_refusal_and_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        quiet,
        "measure_noise",
        lambda: {"spread_pct": 1.25, "median_ops": 1234.0},
    )
    assert quiet.main(["--measure-noise"]) == 0
    assert "A/A spread: 1.25% (median 1,234 ops/s)" in capsys.readouterr().out

    steps = [quiet.Step(0, "pin cores", "affinity")]
    monkeypatch.setattr(quiet, "plan", lambda _tier: steps)
    assert quiet.main(["--tier", "0"]) == 0
    assert "would make 1 change(s)" in capsys.readouterr().out

    def refuse(*_args: object, **_kwargs: object) -> quiet.Journal:
        raise quiet.QuietRefused("unsafe")

    monkeypatch.setattr(quiet, "apply", refuse)
    assert quiet.main(["--tier", "1", "--apply"]) == 2
    assert "REFUSED -- unsafe" in capsys.readouterr().out

    monkeypatch.setattr(
        quiet,
        "apply",
        lambda *_args, **_kwargs: quiet.Journal(
            changes=[quiet.Change("sysfs", "/knob", "0", "1")], watchdog="unit"
        ),
    )
    assert quiet.main(["--tier", "1", "--apply", "--deadline", "45"]) == 0
    output = capsys.readouterr().out
    assert "applied 1 change(s)" in output
    assert "watchdog unit restores in 45s" in output
