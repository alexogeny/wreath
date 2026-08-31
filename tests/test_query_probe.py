from __future__ import annotations

import pytest

from wreath._devtools import query_probe


def test_hidden_child_refuses_unknown_arm_before_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def child(_args: object) -> int:
        raise AssertionError("an invalid child arm reached the workload")

    monkeypatch.setattr(query_probe, "_child_main", child)

    with pytest.raises(SystemExit) as raised:
        query_probe.main(["--dsn", "postgresql://unused", "--run-child", "not-an-arm"])
    assert raised.value.code == 2
    assert "unknown arm" in capsys.readouterr().err


def test_fetchval_is_a_named_probe_arm() -> None:
    assert "fetchval" in query_probe.ARMS
