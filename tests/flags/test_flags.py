from __future__ import annotations

from wreath.flags import FeatureFlags, FlagView, evaluate_rule


def test_boolean_rules():
    for on in ("on", "true", "1", "YES", "enabled"):
        assert evaluate_rule(on, "f") is True
    for off in ("off", "false", "0", "", "no"):
        assert evaluate_rule(off, "f") is False
    assert evaluate_rule("garbage", "f") is False


def test_percentage_is_deterministic():
    # same subject -> same answer across calls; ~monotonic in the threshold
    ctx = {"id": "user-123"}
    a = evaluate_rule("50%", "beta", ctx)
    b = evaluate_rule("50%", "beta", ctx)
    assert a == b
    assert evaluate_rule("0%", "beta", ctx) is False
    assert evaluate_rule("100%", "beta", ctx) is True


def test_percentage_spreads_subjects():
    enabled = sum(evaluate_rule("50%", "beta", {"id": f"user-{i}"}) for i in range(200))
    assert 60 < enabled < 140  # roughly half, not everyone/no-one


def test_from_env_and_enabled():
    flags = FeatureFlags.from_env(
        {"WREATH_FLAG_NEW_UI": "on", "WREATH_FLAG_BETA": "off", "OTHER": "on"}
    )
    assert flags.enabled("new_ui") is True
    assert flags.enabled("NEW_UI") is True  # case-insensitive
    assert flags.enabled("beta") is False
    assert flags.enabled("missing") is False  # unknown flag defaults off
    assert flags.all() == {"new_ui": True, "beta": False}


def test_flag_view():
    flags = FeatureFlags({"new_ui": "on"})
    view = FlagView(flags, {"id": "x"})
    assert view.enabled("new_ui") is True
    assert "new_ui" in view
    assert "missing" not in view
