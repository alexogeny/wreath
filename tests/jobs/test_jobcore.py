"""Pure-logic tests for the jobs/messaging core (no database)."""

from __future__ import annotations

import pytest

from wreath import _jobcore
from wreath._jobcore import (
    CronSchedule,
    PayloadTooLarge,
    TransitionError,
    check_notify_payload,
    check_transition,
    compute_backoff,
    dedup_key,
    valid_transition,
)
from wreath._leased import claim_sql, fenced_update_sql


def test_leased_work_sql_keeps_skip_locked_alias_and_fence_in_one_shape() -> None:
    claim = claim_sql(
        "deliveries",
        key="delivery_id",
        alias="AS d",
        predicate="state='ready'",
        order="run_at",
        limit="$1",
        assignments="state='leased', fence=d.fence+1",
        returning="d.delivery_id, d.fence",
    )
    assert "FOR UPDATE SKIP LOCKED LIMIT $1" in claim
    assert "UPDATE deliveries AS d" in claim
    assert "WHERE d.delivery_id=c.delivery_id" in claim
    assert fenced_update_sql("deliveries", "state='done'") == (
        "UPDATE deliveries SET state='done' WHERE id=$1 AND fence=$2"
    )


def test_legal_transitions():
    assert valid_transition("ready", "leased")
    assert valid_transition("leased", "done")
    assert valid_transition("leased", "ready")
    assert valid_transition("leased", "dead")


def test_illegal_transitions_rejected():
    assert not valid_transition("ready", "done")
    assert not valid_transition("done", "ready")
    assert not valid_transition("dead", "leased")
    assert not valid_transition("ready", "bogus")
    with pytest.raises(TransitionError):
        check_transition("done", "ready")


def test_backoff_exponential_deterministic():
    # No jitter -> deterministic: base * factor**(attempt-1).
    assert compute_backoff(1, kind="exp", base=1.0, factor=2.0) == 1.0
    assert compute_backoff(2, kind="exp", base=1.0, factor=2.0) == 2.0
    assert compute_backoff(4, kind="exp", base=1.0, factor=2.0) == 8.0


def test_backoff_respects_cap():
    assert compute_backoff(20, kind="exp", base=1.0, factor=2.0, cap=10.0) == 10.0


def test_backoff_linear_and_fixed():
    assert compute_backoff(3, kind="linear", base=2.0) == 6.0
    assert compute_backoff(9, kind="fixed", base=5.0) == 5.0


def test_backoff_jitter_is_injectable_and_bounded():
    # jitter_fn=0.5 -> mid-point -> no shift; 0.0/1.0 -> +/- jitter fraction.
    mid = compute_backoff(1, base=10.0, jitter=0.5, jitter_fn=lambda: 0.5)
    low = compute_backoff(1, base=10.0, jitter=0.5, jitter_fn=lambda: 0.0)
    high = compute_backoff(1, base=10.0, jitter=0.5, jitter_fn=lambda: 1.0)
    assert mid == 10.0
    assert low == pytest.approx(5.0)
    assert high == pytest.approx(15.0)


def test_backoff_rejects_bad_attempt_and_kind():
    with pytest.raises(ValueError):
        compute_backoff(0)
    with pytest.raises(ValueError):
        compute_backoff(1, kind="nope")


def test_dedup_key_stable_and_scoped():
    assert dedup_key("q", "a") == dedup_key("q", "a")
    assert dedup_key("q", "a") != dedup_key("q", "b")
    # Same concatenated bytes, different scope boundary -> different key.
    assert dedup_key("q", "ab") != dedup_key("qa", "b")


def test_notify_payload_bound():
    check_notify_payload(b"x" * _jobcore.MAX_NOTIFY_PAYLOAD)
    with pytest.raises(PayloadTooLarge):
        check_notify_payload(b"x" * (_jobcore.MAX_NOTIFY_PAYLOAD + 1))


def test_cron_step_and_range():
    every_15 = CronSchedule("*/15 * * * *")
    assert every_15.matches(minute=0, hour=3, day=1, month=1, weekday=2)
    assert every_15.matches(minute=45, hour=3, day=1, month=1, weekday=2)
    assert not every_15.matches(minute=7, hour=3, day=1, month=1, weekday=2)


def test_cron_weekday_range_monday_to_friday():
    weekdays_9am = CronSchedule("0 9 * * 1-5")
    # Monday (Python weekday 0 -> cron 1) at 09:00 matches.
    assert weekdays_9am.matches(minute=0, hour=9, day=10, month=6, weekday=0)
    # Sunday (Python weekday 6 -> cron 0) does not.
    assert not weekdays_9am.matches(minute=0, hour=9, day=9, month=6, weekday=6)
    # Wrong minute does not.
    assert not weekdays_9am.matches(minute=30, hour=9, day=10, month=6, weekday=0)


def test_cron_rejects_bad_expressions():
    with pytest.raises(ValueError):
        CronSchedule("* * * *")  # too few fields
    with pytest.raises(ValueError):
        CronSchedule("99 * * * *")  # out of range
    with pytest.raises(ValueError):
        CronSchedule("*/0 * * * *")  # zero step
