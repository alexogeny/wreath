"""Cedar selects the shipped evaluator for the active execution mode."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wreath._auth import cedar_engine
from wreath.authorization import CedarPolicies, EntityUid


def _authorize(policies: CedarPolicies) -> Any:
    return policies.is_authorized(
        principal=EntityUid("User", "alice"),
        action=EntityUid("Action", "read"),
        resource=EntityUid("Document", "42"),
    )


def test_native_evaluator_is_selected_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def native(*args: Any) -> tuple[bool, str, tuple[str, ...]]:
        calls.append(args)
        return True, "native evaluator", ()

    def unexpected_pure(*args: Any) -> tuple[bool, str, tuple[str, ...]]:
        raise AssertionError(f"pure evaluator received {args!r}")

    monkeypatch.setattr(
        cedar_engine, "_core", SimpleNamespace(cedar_is_authorized=native)
    )
    monkeypatch.setattr(cedar_engine._pure_cedar, "cedar_is_authorized", unexpected_pure)

    decision = _authorize(CedarPolicies("permit(principal, action, resource);"))

    assert decision.allowed
    assert decision.reason == "native evaluator"
    assert len(calls) == 1


def test_pure_evaluator_is_selected_when_native_module_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def pure(*args: Any) -> tuple[bool, str, tuple[str, ...]]:
        calls.append(args)
        return True, "pure evaluator", ()

    monkeypatch.setattr(cedar_engine, "_core", None)
    monkeypatch.setattr(cedar_engine._pure_cedar, "cedar_is_authorized", pure)

    decision = _authorize(CedarPolicies("permit(principal, action, resource);"))

    assert decision.allowed
    assert decision.reason == "pure evaluator"
    assert len(calls) == 1
