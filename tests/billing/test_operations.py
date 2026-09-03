from __future__ import annotations

import pytest

from wreath.billing.operations import BillingOperations, BillingOperationsUnhealthy
from wreath.metrics import Counters


def test_counters_are_cached_and_read_no_external_state() -> None:
    now = [100.0]
    operations = BillingOperations("stripe", clock=lambda: now[0])

    first = operations.counters()
    operations.webhook_applied()
    operations.webhook_failed()
    operations.reconciliation_completed()
    operations.reconciliation_failed()
    operations.outcome_unknown()
    operations.outcome_dead()
    now[0] = 107.9
    second = operations.counters()

    assert first is second
    assert isinstance(second, Counters)
    assert second.subsystem == "billing"
    assert second.instance == "stripe"
    assert second.values == {
        "webhooks_applied": 1,
        "webhook_failures": 1,
        "reconciliations_completed": 1,
        "reconciliation_failures": 1,
        "unknown_outcomes": 1,
        "dead_outcomes": 1,
        "webhook_lag_seconds": 7,
        "reconciliation_age_seconds": 7,
    }
    assert second.gauges == frozenset(
        {
            "unknown_outcomes",
            "dead_outcomes",
            "webhook_lag_seconds",
            "reconciliation_age_seconds",
        }
    )


def test_outcome_resolution_updates_current_gauges_without_rebuilding_counters() -> None:
    operations = BillingOperations("commerce")
    reading = operations.counters()

    operations.outcome_unknown(2)
    operations.outcome_dead()
    operations.outcome_resolved(unknown=1, dead=1)

    assert operations.counters() is reading
    assert reading.values["unknown_outcomes"] == 1
    assert reading.values["dead_outcomes"] == 0
    with pytest.raises(ValueError, match="exceeds unresolved unknown outcomes"):
        operations.outcome_resolved(unknown=2)


@pytest.mark.asyncio
async def test_alert_is_noncritical_and_names_every_unhealthy_signal() -> None:
    now = [10.0]
    operations = BillingOperations("commerce", clock=lambda: now[0])
    operations.webhook_failed()
    operations.reconciliation_failed()
    operations.outcome_unknown()
    operations.outcome_dead()
    now[0] = 31.0
    alert = operations.alert(webhook_lag=20, reconciliation_age=20)

    assert alert.name == "billing-commerce"
    assert alert.critical is False
    with pytest.raises(BillingOperationsUnhealthy) as caught:
        await alert.probe()

    message = str(caught.value)
    assert "webhook lag" in message
    assert "reconciliation age" in message
    assert "unknown outcomes: 1" in message
    assert "dead outcomes: 1" in message
    assert "webhook failures: 1" in message
    assert "reconciliation failures: 1" in message


@pytest.mark.asyncio
async def test_successful_activity_and_resolved_outcomes_recover_alert() -> None:
    now = [10.0]
    operations = BillingOperations("commerce", clock=lambda: now[0])
    operations.webhook_failed()
    operations.reconciliation_failed()
    operations.outcome_unknown()
    operations.outcome_dead()
    operations.outcome_resolved(unknown=1, dead=1)
    operations.webhook_applied()
    operations.reconciliation_completed()
    now[0] = 15.0

    detail = await operations.alert(webhook_lag=20, reconciliation_age=20).probe()

    assert detail == {
        "webhook_lag_seconds": 5,
        "reconciliation_age_seconds": 5,
        "unknown_outcomes": 0,
        "dead_outcomes": 0,
    }


def test_operations_configuration_is_refused_early() -> None:
    with pytest.raises(ValueError, match="name"):
        BillingOperations("")
    operations = BillingOperations("commerce")
    for value in (0, -1, True, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="webhook_lag"):
            operations.alert(webhook_lag=value, reconciliation_age=10)


def test_outcome_counts_distinguish_positive_updates_from_zero_resolution() -> None:
    operations = BillingOperations("commerce")

    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            operations.outcome_unknown(invalid)

    operations.outcome_resolved(unknown=0, dead=0)
