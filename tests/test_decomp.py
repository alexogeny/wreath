"""`wreath-decomp` and the shared measurement harness.

These assert the harness's guarantees, not timings: a benchmark's numbers are
not a test, but "every arm served a 200" and "the A/A control is at the far end
of the round" are the invariants that make its numbers mean anything.
"""

from __future__ import annotations

import asyncio

import pytest

from wreath._devtools import decomp, measure


def test_the_stage_apps_all_serve_the_traced_route() -> None:
    # An arm that 403s or 404s still produces timings -- of the error path.
    template = measure.scope("GET", "/users/1", decomp.REQUEST_HEADERS)
    for auth in (False, True):
        for orm in (False, True):
            app = decomp._build_stage_app(auth=auth, policy=auth, orm=orm)
            status = asyncio.run(measure.status_of(app, template))
            assert status == 200, f"auth={auth} orm={orm} answered {status}"


def test_the_stages_differ_only_in_the_stage_under_test() -> None:
    plain = decomp._build_stage_app(auth=False, policy=False, orm=False)
    authed = decomp._build_stage_app(auth=True, policy=False, orm=False)
    assert plain._auth_backend is None
    assert authed._auth_backend is not None
    # No global middleware in any arm: this suite prices what the tape is not.
    for app in (plain, authed):
        assert app._global_middleware == []


def test_verify_serving_rejects_an_arm_that_stopped_serving() -> None:
    app = decomp._build_stage_app(auth=True, policy=True, orm=False)
    arm = measure.Arm("authed", app)
    # No credentials: the route is protected, so this arm answers 401.
    template = measure.scope("GET", "/users/1", {"host": "example.com"})
    with pytest.raises(SystemExit, match="not 200"):
        asyncio.run(measure.verify_serving([arm], template, "before"))


def test_verify_serving_accepts_a_serving_arm() -> None:
    app = decomp._build_stage_app(auth=True, policy=True, orm=True)
    arm = measure.Arm("full", app)
    template = measure.scope("GET", "/users/1", decomp.REQUEST_HEADERS)
    asyncio.run(measure.verify_serving([arm], template, "before"))


def test_the_noise_floor_comes_from_two_separate_arms() -> None:
    base = measure.Arm("base")
    base.samples = [10.0, 10.0, 10.0]
    control = measure.Arm("control")
    control.samples = [10.5, 10.5, 10.5]
    assert measure.noise_floor([base, control], "base", "control") == pytest.approx(0.5)


def test_a_delta_below_the_floor_is_reported_as_unresolved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = measure.Arm("base")
    base.samples = [10.0]
    small = measure.Arm("small")
    small.samples = [10.2]  # +0.2, under 2x the 0.5 floor
    control = measure.Arm("control")
    control.samples = [10.5]
    result = measure.report([base, small, control], "base", "control")
    assert result["rows"][0]["resolved"] is False
    assert "BELOW NOISE" in capsys.readouterr().out


def test_a_delta_above_the_floor_is_reported_as_resolved() -> None:
    base = measure.Arm("base")
    base.samples = [10.0]
    big = measure.Arm("big")
    big.samples = [20.0]
    control = measure.Arm("control")
    control.samples = [10.1]
    result = measure.report([base, big, control], "base", "control")
    assert result["rows"][0]["resolved"] is True


def test_a_frame_chain_actually_costs_frames() -> None:
    # The calibration's slope is meaningless if the compiler folds the chain away.
    import sys

    depth_counts: list[int] = []
    for depth in (0, 5):
        chain = decomp._frame_chain(depth)
        seen = 0

        def counter(frame: object, event: str, arg: object) -> None:
            nonlocal seen
            if event == "call":
                seen += 1

        sys.setprofile(counter)
        chain(0)
        sys.setprofile(None)
        depth_counts.append(seen)
    assert depth_counts[1] - depth_counts[0] == 5


def test_every_named_suite_is_reachable() -> None:
    assert set(decomp.SUITES) == {"request", "orm", "calibrate"}


def test_the_orm_arms_name_the_hydration_path_they_measured() -> None:
    """The ORM arms must not report the fallback as if it were the read.

    `Session._hydrate_plan` returns a native plan only for a connection that
    installs `_decode_dest`, and no scripted double does. So `full fetch_one`
    measures `Session._hydrate` -- a Python pass per column per row -- while a
    deployment decodes straight into the model's cells. Measured on 10,000 real
    rows the gap is ~3.7x, which is far too large for the suite to leave
    unsaid: every ratio printed against that arm inherits the error.
    """
    from wreath._devtools.sample_app import _ScriptedDatabase

    assert decomp._hydration_path(_ScriptedDatabase()) == "record"


def test_a_connection_that_decodes_natively_is_named_as_such() -> None:
    """The probe reads the same hook the session gates on, not a hard-coded answer."""

    class _Nativeish:
        connection = type("C", (), {"_decode_dest": object()})()

    assert decomp._hydration_path(_Nativeish()) == "native"
