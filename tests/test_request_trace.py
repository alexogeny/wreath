from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._devtools import request_trace
from wreath._devtools.sample_app import SCENARIOS


def _trace(scenario: str) -> tuple[request_trace.Trace, int]:
    app, headers, method, path = SCENARIOS[scenario]()
    return request_trace.trace_request(app, method, path, headers)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_scenario_serves_its_own_request(scenario: str) -> None:
    # A scenario that 403s or 404s would still produce a trace, and the numbers
    # would quietly describe the error path instead of the lifecycle.
    _events, status = _trace(scenario)
    assert status == 200


def test_the_traced_request_reaches_its_handler() -> None:
    trace, _status = _trace("realistic")
    assert any(event.phase == "handler" for event in trace.events)


def test_handler_codes_include_only_real_declared_and_compiled_callables() -> None:
    def declared() -> None:
        pass

    def compiled() -> None:
        pass

    app = SimpleNamespace(
        _routes=(
            SimpleNamespace(handler=declared),
            SimpleNamespace(handler=object()),
        ),
        _handler_requirements={compiled: object(), "not-callable": object()},
    )

    assert request_trace._handler_codes(app) == frozenset(
        {declared.__code__, compiled.__code__}
    )


def test_drive_takes_status_only_from_a_response_start() -> None:
    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.body", "status": 599, "body": b""})
        await send({"type": "http.response.start", "status": 204})

    _trace_result, status = asyncio.run(request_trace._drive(app, "GET", "/", {}))

    assert status == 204


def test_the_harness_is_not_counted_against_the_app() -> None:
    trace, _status = _trace("realistic")
    # Building the scope and collecting sent messages are this tool's work, and
    # counting them once inflated ingress by an order of magnitude.
    assert not any("request_trace.py" in name for name in trace.py_calls)


def test_a_bare_route_crosses_far_less_than_a_full_stack() -> None:
    minimal, _ = _trace("minimal")
    realistic, _ = _trace("realistic")

    def pre_activation(trace: request_trace.Trace) -> int:
        return sum(1 for event in trace.events if event.phase in request_trace._PRE_ACTIVATION)

    # The full stack has the same single Python entry but substantially more
    # Python-to-C boundaries for routing, policy, and authorization.
    assert pre_activation(minimal) < pre_activation(realistic)


def test_counting_a_request_twice_gives_the_same_answer() -> None:
    # The baseline is only worth checking in if it does not drift on its own.
    first, _ = _trace("realistic")
    second, _ = _trace("realistic")
    assert first.py_calls == second.py_calls
    assert first.c_calls == second.c_calls


def test_the_checked_in_baseline_matches_what_the_tracer_measures() -> None:
    path = request_trace._baseline_path()
    assert path.exists(), "run: uv run wreath-request-trace --update-baseline"
    recorded = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    current = request_trace._measure_scenarios()["scenarios"]
    for name, summary in current.items():
        assert name in recorded, f"{name} is missing from the baseline"
        assert summary["pre_activation"] == recorded[name]["pre_activation"], (
            f"{name} moved off its baseline; if that is intended, re-record with "
            "--update-baseline and say why in the commit"
        )
        assert summary["totals"] == recorded[name]["totals"]


def test_check_reports_a_scenario_that_grew(monkeypatch: pytest.MonkeyPatch) -> None:
    measure = request_trace._measure_scenarios

    def grown() -> dict[str, Any]:
        payload = measure()
        payload["scenarios"]["minimal"]["pre_activation"]["python"] += 5
        return payload

    monkeypatch.setattr(request_trace, "_measure_scenarios", grown)
    assert request_trace._check_baseline() == 1


def _summary(*, python: int = 1, c: int = 2) -> dict[str, Any]:
    return {
        "pre_activation": {"python": python, "c": c},
        "totals": {"python": python + 1, "c": c + 1},
    }


def test_check_refuses_a_missing_baseline(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(request_trace, "_baseline_path", lambda: tmp_path / "missing.json")

    result = request_trace._check_baseline()

    assert type(result) is int
    assert result == 1
    assert "no baseline" in capsys.readouterr().err


def test_check_reports_a_scenario_missing_from_the_baseline(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"scenarios": {}}))
    monkeypatch.setattr(request_trace, "_baseline_path", lambda: path)
    monkeypatch.setattr(
        request_trace,
        "_measure_scenarios",
        lambda: {"scenarios": {"new-scenario": _summary()}},
    )

    assert request_trace._check_baseline() == 1
    assert "new-scenario: not in the baseline" in capsys.readouterr().out


def test_check_accepts_an_exact_match_without_reporting_fake_changes(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = _summary()
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"scenarios": {"minimal": summary}}))
    monkeypatch.setattr(request_trace, "_baseline_path", lambda: path)
    monkeypatch.setattr(
        request_trace,
        "_measure_scenarios",
        lambda: {"scenarios": {"minimal": _summary()}},
    )

    result = request_trace._check_baseline()
    captured = capsys.readouterr()

    assert type(result) is int
    assert result == 0
    assert "no crossings added" in captured.out
    assert "grew" not in captured.out
    assert "shrank" not in captured.out
    assert captured.err == ""


def test_check_and_update_are_refused_together(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_trace, "_baseline_path", lambda: tmp_path / "baseline.json")
    with pytest.raises(SystemExit, match="exclusive"):
        request_trace.main(["--check", "--update-baseline"])


def test_check_and_update_each_run_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_trace, "_check_baseline", lambda: 7)
    monkeypatch.setattr(request_trace, "_write_baseline", lambda: 8)

    assert request_trace.main(["--check"]) == 7
    assert request_trace.main(["--update-baseline"]) == 8
