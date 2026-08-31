from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import _cli


def _progress(**overrides: object) -> SimpleNamespace:
    values = {
        "percent": 50.0,
        "denominator_kind": "estimated",
        "eta_seconds": 30.0,
        "state_reason": "waiting for input",
        "eta_absent": "no rate yet",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pass_row(**overrides: object) -> SimpleNamespace:
    values = {
        "name": "backfill",
        "tenant": "tenant-a",
        "state": "blocked",
        "progress": _progress(),
        "rows_done": 12,
        "last_error": "chunk failed",
        "trace_id": "trace-1",
        "holes_open": 2,
        "pending": 3,
        "verified_at": "2026-08-30",
        "verified_fact": "users-ready",
        "guards": ("users-ready",),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _capture_namespace(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "token": "token",
        "socket": "inspector.sock",
        "capture_action": "arm",
        "arm_id": 7,
        "allow_headers": [],
        "hash_headers": [],
        "mask_headers": [],
        "allow_query": [],
        "hash_query": [],
        "mask_query": [],
        "body": None,
        "dependency": None,
        "max_body_bytes": 0,
        "max_fields": 0,
        "max_depth": 0,
        "slab_bytes": 4096,
        "slabs": 0,
        "expiry": 60,
        "max_matches": 4,
        "as_json": True,
    }
    values.update(overrides)
    return Namespace(**values)


def test_pass_printer_renders_every_diagnostic_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli._print_passes([_pass_row()])
    output = capsys.readouterr().out

    assert "backfill@tenant-a" in output
    assert "waiting for input" in output
    assert "last chunk error: chunk failed" in output
    assert "trace: trace-1" in output
    assert "no ETA: no rate yet" in output
    assert "2 dead-lettered chunk(s)" in output
    assert "3 unit(s) queued" in output
    assert "verified 2026-08-30: published users-ready" in output


def test_pass_printer_omits_absent_diagnostics_and_reports_unpublished_guard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _pass_row(
        tenant="",
        state="running",
        progress=_progress(state_reason="", eta_absent=""),
        last_error="",
        trace_id="",
        holes_open=0,
        pending=0,
        verified_at=None,
        verified_fact=None,
        guards=("users-ready",),
    )

    _cli._print_passes([row])
    output = capsys.readouterr().out

    assert "backfill@" not in output
    assert "last chunk error" not in output
    assert "trace:" not in output
    assert "no ETA:" not in output
    assert "dead-lettered" not in output
    assert "queued" not in output
    assert "guards ('users-ready',), not yet published" in output
    assert len(output.splitlines()) == 4


def test_pass_printer_does_not_repeat_an_error_already_present_in_the_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _pass_row(
        progress=_progress(state_reason="chunk failed while loading"),
        last_error="chunk failed",
    )

    _cli._print_passes([row])

    assert "last chunk error" not in capsys.readouterr().out


def test_pass_printer_handles_no_rows(capsys: pytest.CaptureFixture[str]) -> None:
    _cli._print_passes([])
    assert capsys.readouterr().out == "no passes have run yet\n"


def test_hole_printer_distinguishes_tenant_and_plain_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    holes = [
        SimpleNamespace(
            name="first",
            tenant="",
            attempts=2,
            failed_at="now",
            error="broken",
            predicate="id > 1",
        ),
        SimpleNamespace(
            name="second",
            tenant="tenant-b",
            attempts=3,
            failed_at="later",
            error="still broken",
            predicate="id > 2",
        ),
    ]

    _cli._print_holes(holes)
    output = capsys.readouterr().out

    assert "first  after 2 attempt(s)" in output
    assert "second@tenant-b  after 3 attempt(s)" in output


@pytest.mark.parametrize(
    ("action", "body", "expected"),
    [
        (
            "arm",
            {"arm_id": 1, "expires_in": 30, "remaining_matches": -1, "headers": []},
            "unlimited matches\n  headers: none",
        ),
        (
            "arm",
            {"arm_id": 2, "expires_in": 40, "remaining_matches": 3, "headers": ["host"]},
            "3 matches\n  headers: host",
        ),
        ("disarm", {"disarmed": True}, "disarmed"),
        ("disarm", {"disarmed": False}, "no such arm"),
    ],
)
def test_capture_printer_renders_each_action(
    capsys: pytest.CaptureFixture[str],
    action: str,
    body: dict[str, Any],
    expected: str,
) -> None:
    _cli._print_capture(action, body)
    assert expected in capsys.readouterr().out


def test_capture_status_printer_renders_empty_and_populated_header_sets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli._print_capture(
        "status",
        {
            "ceiling": {"capture_slabs": 8, "max_capture_bytes": 8192, "body": True},
            "arms": [
                {
                    "arm_id": 1,
                    "expires_in": 10,
                    "remaining_matches": -1,
                    "headers": [],
                },
                {
                    "arm_id": 2,
                    "expires_in": 20,
                    "remaining_matches": 2,
                    "headers": ["host", "accept"],
                },
            ],
        },
    )
    output = capsys.readouterr().out

    assert "ceiling: 8 slabs, 8192 bytes, body=True" in output
    assert "#1: expires in 10s, unlimited matches, headers none" in output
    assert "#2: expires in 20s, 2 matches, headers host, accept" in output


class _InspectorDouble:
    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, _socket: str) -> None:
        pass

    async def __aenter__(self) -> _InspectorDouble:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def capture_status(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("status", kwargs))
        return {"state": "ready"}

    async def disarm_capture(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("disarm", kwargs))
        return {"disarmed": True}

    async def arm_capture(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("arm", kwargs))
        return {"arm_id": 9}


@pytest.fixture
def inspector_double(monkeypatch: pytest.MonkeyPatch) -> type[_InspectorDouble]:
    from wreath import inspector

    _InspectorDouble.calls = []
    monkeypatch.setattr(inspector, "InspectorClient", _InspectorDouble)
    return _InspectorDouble


def test_capture_arm_projects_every_redaction_and_budget_field(
    inspector_double: type[_InspectorDouble],
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = _capture_namespace(
        allow_headers=["host"],
        hash_headers=["authorization"],
        mask_headers=["cookie"],
        allow_query=["page"],
        hash_query=["token"],
        mask_query=["secret"],
        body=True,
        dependency=False,
        max_body_bytes=1024,
        max_fields=12,
        max_depth=4,
        slabs=3,
    )

    assert _cli.execute_capture(namespace) == 0
    report = json.loads(capsys.readouterr().out)

    assert report == {"version": 1, "action": "arm", "data": {"arm_id": 9}}
    assert inspector_double.calls == [
        (
            "arm",
            {
                "token": "token",
                "redaction": {
                    "header_allowlist": ["host"],
                    "header_hash": ["authorization"],
                    "header_mask": ["cookie"],
                    "query_allowlist": ["page"],
                    "query_hash": ["token"],
                    "query_mask": ["secret"],
                    "body": True,
                    "dependency": False,
                    "max_body_bytes": 1024,
                    "max_fields": 12,
                    "max_depth": 4,
                },
                "budget": {"slab_bytes": 4096, "slabs": 3},
                "expiry_seconds": 60,
                "max_matches": 4,
            },
        )
    ]


def test_capture_arm_uses_none_for_empty_redaction_and_omits_zero_slab_count(
    inspector_double: type[_InspectorDouble],
) -> None:
    assert _cli.execute_capture(_capture_namespace()) == 0

    assert inspector_double.calls[0][1]["redaction"] is None
    assert inspector_double.calls[0][1]["budget"] == {"slab_bytes": 4096}


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("status", ("status", {"token": "token"})),
        ("disarm", ("disarm", {"token": "token", "arm_id": 7})),
    ],
)
def test_capture_dispatches_status_and_disarm(
    inspector_double: type[_InspectorDouble],
    action: str,
    expected: tuple[str, dict[str, object]],
) -> None:
    assert _cli.execute_capture(_capture_namespace(capture_action=action)) == 0
    assert inspector_double.calls == [expected]


def test_capture_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WREATH_CAPTURE_TOKEN", raising=False)

    with pytest.raises(_cli.CliError, match="capability token is required"):
        _cli.execute_capture(_capture_namespace(token=None))


def test_capture_accepts_the_environment_token(
    inspector_double: type[_InspectorDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_CAPTURE_TOKEN", "environment-token")

    assert _cli.execute_capture(_capture_namespace(token=None, capture_action="status")) == 0
    assert inspector_double.calls == [("status", {"token": "environment-token"})]


def test_inspect_summary_filters_zero_losses(capsys: pytest.CaptureFixture[str]) -> None:
    _cli._print_inspect(
        "summary",
        {
            "server": {"protocol": 1, "pid": 42, "capabilities": ["routes"]},
            "workers": [
                {
                    "mode": "threaded",
                    "requests": 5,
                    "completions": 4,
                    "active_count": 1,
                    "ring_occupancy": 2,
                    "ring_high_water": 3,
                    "phase_in_use": 1,
                    "phase_capacity": 8,
                    "phase_high_water": 4,
                    "losses": {"events": 0, "frames": 2},
                }
            ],
        },
    )
    output = capsys.readouterr().out

    assert "capabilities: routes" in output
    assert "losses: {'frames': 2}" in output
    assert "events" not in output


def test_inspect_summary_reports_none_when_every_loss_is_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli._print_inspect(
        "summary",
        {
            "server": {"protocol": 1, "pid": 42, "capabilities": []},
            "workers": [
                {
                    "mode": "threaded",
                    "requests": 0,
                    "completions": 0,
                    "active_count": 0,
                    "ring_occupancy": 0,
                    "ring_high_water": 0,
                    "phase_in_use": 0,
                    "phase_capacity": 8,
                    "phase_high_water": 0,
                    "losses": {"events": 0},
                }
            ],
        },
    )

    assert "losses: none" in capsys.readouterr().out


@pytest.mark.parametrize("truncated", [False, True])
def test_inspect_active_marks_only_truncated_pages(
    capsys: pytest.CaptureFixture[str],
    truncated: bool,
) -> None:
    _cli._print_inspect(
        "active",
        {
            "total": 1,
            "truncated": truncated,
            "requests": [
                {
                    "request_id": 4,
                    "protocol": "http/1.1",
                    "age_us": 8,
                    "route_id": 3,
                }
            ],
        },
    )
    output = capsys.readouterr().out

    assert ("[truncated page]" in output) is truncated
    assert "#4" in output


def test_inspect_routes_renders_route_and_named_metadata_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli._print_inspect(
        "routes",
        {
            "table": "routes",
            "total": 2,
            "truncated": False,
            "rows": [
                {"id": 1, "method": "GET", "path": "/users"},
                {"id": 2, "name": "fallback"},
            ],
        },
    )
    output = capsys.readouterr().out

    assert "GET     /users" in output
    assert "fallback" in output
    assert "[truncated page]" not in output


def test_inspect_routes_marks_a_truncated_page(capsys: pytest.CaptureFixture[str]) -> None:
    _cli._print_inspect(
        "routes",
        {"table": "routes", "total": 0, "truncated": True, "rows": []},
    )
    assert "[truncated page]" in capsys.readouterr().out


@pytest.mark.parametrize("topic", ["timeline", "failures"])
def test_inspect_traces_render_failure_flags_phases_and_loss(
    capsys: pytest.CaptureFixture[str],
    topic: str,
) -> None:
    _cli._print_inspect(
        topic,
        {
            "total": 2,
            "assembled": 2,
            "truncated": True,
            "traces": [
                {
                    "request_id": 1,
                    "protocol": "http/1.1",
                    "status": 500,
                    "terminal": "response",
                    "duration_us": 20,
                    "route_id": 4,
                    "is_failure": True,
                    "phases": ["route"],
                },
                {
                    "request_id": 2,
                    "protocol": "h2",
                    "status": 204,
                    "terminal": "complete",
                    "duration_us": 10,
                    "route_id": 5,
                    "is_failure": False,
                    "phases": [],
                },
            ],
            "loss": {"frames": 1, "events": 0},
        },
    )
    output = capsys.readouterr().out

    assert ("failure(s)" in output) is (topic == "failures")
    assert "!#1" in output
    assert "!#2" not in output
    assert "phases=1" in output
    assert "phases=0" not in output
    assert "projector loss: {'frames': 1}" in output


def test_inspect_timeline_omits_the_truncation_marker_when_complete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli._print_inspect(
        "timeline",
        {"total": 0, "assembled": 0, "truncated": False, "traces": [], "loss": None},
    )
    assert "[truncated page]" not in capsys.readouterr().out


def test_inspect_distributions_renders_paths_route_ids_and_zero_count_average(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli._print_inspect(
        "distributions",
        {
            "assembled": 3,
            "routes": [
                {
                    "method": "GET",
                    "path": "/users",
                    "route_id": 1,
                    "count": 2,
                    "errors": 1,
                    "duration_us_sum": 30,
                    "duration_us_max": 20,
                },
                {
                    "method": "POST",
                    "path": "",
                    "route_id": 2,
                    "count": 0,
                    "errors": 0,
                    "duration_us_sum": 99,
                    "duration_us_max": 0,
                },
            ],
            "loss": None,
        },
    )
    output = capsys.readouterr().out

    assert "GET /users: 2 req, 1 err, avg 15us" in output
    assert "route 2: 0 req, 0 err, avg 0us" in output


def test_inspect_unknown_topic_prints_key_value_rows(capsys: pytest.CaptureFixture[str]) -> None:
    _cli._print_inspect("custom", {"one": 1, "two": "second"})
    assert capsys.readouterr().out == "one: 1\ntwo: second\n"


def test_capability_renderer_distinguishes_builtin_and_module_backed_rows() -> None:
    builtin = SimpleNamespace(
        capability=SimpleNamespace(name="core", sentence="Built in support.", modules=()),
        reason="name",
        matched="core",
    )
    module = SimpleNamespace(
        capability=SimpleNamespace(
            name="crypto",
            sentence="Cryptographic support.",
            modules=("cryptography",),
        ),
        reason="module",
        matched="cryptography",
    )

    assert "modules  built in" in _cli._render_capability(builtin)
    assert "modules  cryptography" in _cli._render_capability(module)


def test_capabilities_index_renders_builtin_and_module_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wreath import _capabilities

    rows = [
        SimpleNamespace(name="core", sentence="Core.", modules=(), replaces=()),
        SimpleNamespace(
            name="crypto",
            sentence="Crypto.",
            modules=("cryptography",),
            replaces=("package",),
        ),
    ]
    monkeypatch.setattr(_capabilities, "index", lambda: rows)

    assert _cli.execute_capabilities(Namespace(term=None, as_json=False)) == 0
    output = capsys.readouterr().out

    assert "core             built in" in output
    assert "crypto           cryptography" in output
    assert "2 capabilities" in output


def test_capabilities_search_distinguishes_zero_one_and_multiple_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wreath import _capabilities

    capability = SimpleNamespace(name="core", sentence="Core.", modules=(), replaces=())
    match = SimpleNamespace(
        capability=capability,
        reason="name",
        matched="core",
    )
    matches: list[object] = []
    monkeypatch.setattr(_capabilities, "lookup", lambda _term: matches)

    assert _cli.execute_capabilities(Namespace(term="missing", as_json=False)) == 1
    assert "nothing here answers 'missing'" in capsys.readouterr().err

    matches.append(match)
    assert _cli.execute_capabilities(Namespace(term="core", as_json=False)) == 0
    assert "core -- 1 capability\n" in capsys.readouterr().out

    matches.append(match)
    assert _cli.execute_capabilities(Namespace(term="core", as_json=False)) == 0
    assert "core -- 2 capabilities\n" in capsys.readouterr().out
