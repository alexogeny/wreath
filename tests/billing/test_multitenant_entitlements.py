from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wreath._auth.cedar_engine import CedarPolicies
from wreath.auth import Identity
from wreath.authorization import CedarAuthorizer
from wreath.subscriptions import (
    Plan,
    PlanCatalog,
    SubscriptionEntitlements,
    SubscriptionSnapshot,
    SubscriptionState,
)


class State:
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class Request:
    method = "GET"
    path = "/exports"

    def __init__(self, tenant: str) -> None:
        self.identity = Identity("alice")
        self.state = State()
        self.tenant = tenant


def test_cedar_resolves_subscription_entitlements_for_the_active_request_tenant() -> None:
    paid = datetime(2026, 10, 1, tzinfo=UTC)
    subscriptions = {
        "acme": SubscriptionSnapshot(
            "stripe",
            "sub_acme",
            "organization:acme",
            "pro",
            SubscriptionState.ACTIVE,
            "active",
            paid_through=paid,
        ),
        "globex": SubscriptionSnapshot(
            "stripe",
            "sub_globex",
            "organization:globex",
            "free",
            SubscriptionState.ACTIVE,
            "active",
            paid_through=paid,
        ),
    }
    provider = SubscriptionEntitlements(
        PlanCatalog(
            Plan("free", "price_free"),
            Plan("pro", "price_pro", entitlements=frozenset({"export"})),
        ),
        subscription_for_request=lambda request: subscriptions[request.tenant],
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    authorizer = CedarAuthorizer(
        engine=CedarPolicies(
            'permit(principal, action, resource) when { context.entitlements.contains("export") };'
        ),
        entitlements=provider,
    )

    assert authorizer.facts_for(Request("acme"))["entitlements"] == frozenset({"export"})
    assert authorizer.facts_for(Request("globex"))["entitlements"] == frozenset()
