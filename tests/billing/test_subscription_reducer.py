from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from wreath._auth.cedar_engine import CedarPolicies
from wreath._auth.principal import human, on_plan
from wreath.auth import Identity
from wreath.authorization import CedarAuthorizer
from wreath.subscriptions import (
    AccessPolicy,
    Plan,
    PlanCatalog,
    SubscriptionEntitlements,
    SubscriptionLedger,
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)


def test_access_policy_is_validated_and_frozen_at_declaration() -> None:
    mutable = {SubscriptionState.ACTIVE}
    policy = AccessPolicy(mutable)
    mutable.add(SubscriptionState.PAST_DUE)

    assert policy.granted == frozenset({SubscriptionState.ACTIVE})
    with pytest.raises(TypeError, match="SubscriptionState members"):
        AccessPolicy({"active"})


PAID_THROUGH = datetime(2026, 10, 1, tzinfo=UTC)


def snapshot(
    *, provider: str = "stripe", subject: str = "organization:acme"
) -> SubscriptionSnapshot:
    return SubscriptionSnapshot(
        provider,
        "sub_1",
        subject,
        "pro",
        SubscriptionState.ACTIVE,
        "active",
    )


def payment(*, provider: str = "stripe", subject: str = "organization:acme") -> SubscriptionPayment:
    return SubscriptionPayment(
        provider,
        "in_1",
        "sub_1",
        subject,
        PAID_THROUGH,
    )


@pytest.mark.parametrize("first", ["snapshot", "payment"])
def test_subscription_payment_and_snapshot_reduce_to_the_same_paid_projection(
    first: str,
) -> None:
    ledger = SubscriptionLedger()
    values = (snapshot(), payment()) if first == "snapshot" else (payment(), snapshot())

    for value in values:
        ledger.apply(value)

    assert ledger.get("stripe", "sub_1", "organization:acme") == SubscriptionSnapshot(
        "stripe",
        "sub_1",
        "organization:acme",
        "pro",
        SubscriptionState.ACTIVE,
        "active",
        paid_through=PAID_THROUGH,
    )


def test_payment_before_subscription_is_preserved_until_the_snapshot_arrives() -> None:
    ledger = SubscriptionLedger()

    assert ledger.apply(payment()) is None
    merged = ledger.apply(snapshot())
    assert merged is not None
    assert merged.paid_through == PAID_THROUGH


@pytest.mark.parametrize(
    ("later", "message"),
    [
        (snapshot(subject="organization:globex"), "subject"),
        (snapshot(provider="chargebee"), "provider"),
    ],
)
def test_subscription_ledger_refuses_contradictory_ownership(
    later: SubscriptionSnapshot, message: str
) -> None:
    ledger = SubscriptionLedger()
    ledger.apply(payment())

    with pytest.raises(ValueError, match=message):
        ledger.apply(later)


def test_subscription_entitlements_resolve_plan_and_grants_atomically() -> None:
    calls = 0

    def changing(_identity: Any) -> SubscriptionSnapshot:
        nonlocal calls
        calls += 1
        return SubscriptionSnapshot(
            "stripe",
            f"sub_{calls}",
            "user:alice",
            "pro" if calls == 1 else "basic",
            SubscriptionState.ACTIVE,
            "active",
            paid_through=PAID_THROUGH,
        )

    provider = SubscriptionEntitlements(
        PlanCatalog(
            Plan("pro", "price_pro", frozenset({"export"})),
            Plan("basic", "price_basic", frozenset({"basic"})),
        ),
        subscription_for=changing,
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )

    resolution = provider.resolve(Identity("alice"))

    assert resolution.plan == "pro"
    assert resolution.entitlements == frozenset({"export"})
    assert calls == 1


class State:
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class Request:
    method = "GET"
    path = "/exports"

    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.state = State()


def test_cedar_cannot_mix_a_plan_and_entitlements_from_different_snapshots() -> None:
    calls = 0

    def changing(_identity: Any) -> SubscriptionSnapshot:
        nonlocal calls
        calls += 1
        return SubscriptionSnapshot(
            "stripe",
            f"sub_{calls}",
            "user:alice",
            "pro" if calls == 1 else "basic",
            SubscriptionState.ACTIVE,
            "active",
            paid_through=PAID_THROUGH,
        )

    provider = SubscriptionEntitlements(
        PlanCatalog(
            Plan("pro", "price_pro", frozenset({"export"})),
            Plan("basic", "price_basic", frozenset({"basic"})),
        ),
        subscription_for=changing,
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    authorizer = CedarAuthorizer(
        engine=CedarPolicies(
            'permit(principal, action, resource) when { context.entitlements.contains("export") };'
        ),
        entitlements=provider,
    )
    identity = (human(Identity("alice")) | on_plan("pro")).bind()

    request: Any = Request(identity)
    assert authorizer.facts_for(request)["entitlements"] == frozenset({"export"})
    assert calls == 1
