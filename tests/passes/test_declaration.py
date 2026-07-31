"""Refusals that fire where the pass is declared, not at three in the morning.

Each of these is a bug the declaration can see: a chunk that cannot fit inside a
shift, a shift that cannot fit inside a lease, a key the work itself moves, a
callback nobody has claimed is safe to run twice. Raising at import time costs a
failed start; the same bug raising during a walk costs a table.
"""

from __future__ import annotations

import pytest

from wreath.passes import (
    Apply,
    Ceiling,
    ChunkedPass,
    Declared,
    DutyCycle,
    Key,
    PassDeclarationError,
    Purge,
    Rewrite,
    Rows,
    Sealed,
    Table,
)

EXPIRES = Key("expires", "timestamptz", indexed=True)
KEY = Key("key", "text", unique=True)
ID = Key("id", "int8", indexed=True, unique=True, monotone=True)


def declare(**overrides):
    name = overrides.pop("name", "purge_replays")
    options = {
        "over": Table("replays"),
        "units": Rows(key=(EXPIRES, KEY), limit=100, within="2s"),
        "frontier": Sealed(),
        "work": Purge(),
    }
    options.update(overrides)
    return ChunkedPass(name, **options)


# --- the time chain -----------------------------------------------------------


def test_a_chunk_budget_must_fit_inside_a_shift():
    with pytest.raises(PassDeclarationError) as error:
        declare(units=Rows(key=(EXPIRES, KEY), limit=100, within="30s"), shift="10s")

    message = " ".join(str(error.value).split())
    assert "must fit inside a shift" in message
    # The whole chain is worth restating in the message, because getting one
    # link wrong is indistinguishable from getting any other link wrong.
    assert "statement_timeout < within < shift < lease < command_timeout" in message


def test_a_chunk_budget_equal_to_the_shift_is_refused_too():
    # Equal is not "fits": the shift would end exactly as the chunk timed out,
    # and which one wins is a race rather than a decision.
    with pytest.raises(PassDeclarationError, match="must fit inside a shift"):
        declare(units=Rows(key=(EXPIRES, KEY), within="10s"), shift="10s")


def test_a_shift_longer_than_the_lease_is_refused_by_the_job_runner(jobs_runner):
    walk = declare(shift="60s")

    with pytest.raises(ValueError) as error:
        jobs_runner.drive(walk, cron="*/5 * * * *")

    message = " ".join(str(error.value).split())
    assert "leases jobs for" in message
    # There is no heartbeat, so a handler still running when its lease expires is
    # reclaimed while it runs and a second worker picks it up.
    assert "reclaimed while it runs" in message


def test_a_shift_shorter_than_the_lease_is_accepted(jobs_runner):
    walk = declare(shift="10s")

    assert jobs_runner.drive(walk, cron="*/5 * * * *")


def test_a_recurring_pass_without_a_schedule_is_refused(jobs_runner):
    # Nothing else would start the next cycle, and a pass that quietly stops
    # after one cycle is worse than one that refuses to be declared.
    with pytest.raises(ValueError, match="nothing would start the next one"):
        jobs_runner.drive(declare())


def test_one_runner_refuses_to_drive_the_same_pass_twice(jobs_runner):
    jobs_runner.drive(declare(), cron="*/5 * * * *")

    with pytest.raises(ValueError, match="already driven"):
        jobs_runner.drive(declare(), cron="*/5 * * * *")


@pytest.mark.parametrize("bad", ["0s", "-1s", "nonsense", None])
def test_a_shift_must_be_a_positive_duration(bad):
    with pytest.raises(PassDeclarationError):
        declare(shift=bad)


def test_a_duration_may_be_written_in_any_of_the_usual_units():
    assert declare(shift="10s").shift == 10.0
    assert declare(shift="250ms", units=Rows(key=(EXPIRES, KEY), within="100ms")).shift == 0.25
    assert declare(shift="5m").shift == 300.0
    assert declare(shift=12).shift == 12.0


def test_duration_parser_distinguishes_bool_unitless_text_and_permitted_zero():
    from wreath._passes.duration import seconds

    assert seconds("2", what="shift") == 2.0
    with pytest.raises(PassDeclarationError, match="must be a duration"):
        seconds(True, what="shift")
    with pytest.raises(PassDeclarationError, match="must be positive"):
        seconds(0, what="shift")
    assert seconds(0, what="frontier", allow_zero=True) == 0.0


# --- the work and the key -----------------------------------------------------


def test_a_pass_refuses_to_walk_by_a_column_its_own_work_rewrites():
    # A key the work changes moves rows past the cursor, so they are processed
    # twice or never -- and the counters still add up either way.
    with pytest.raises(PassDeclarationError) as error:
        declare(
            units=Rows(key=(EXPIRES, KEY), within="2s"),
            work=Rewrite({"expires": "now()"}),
        )

    message = " ".join(str(error.value).split())
    assert "its work writes expires" in message
    assert "processed twice or never" in message


def test_work_that_writes_a_non_key_column_is_fine():
    walk = declare(work=Rewrite({"payload": "'{}'::jsonb"}))

    assert walk.work.writes == ("payload",)


def test_a_rewrite_needs_at_least_one_column():
    with pytest.raises(PassDeclarationError, match="at least one column"):
        Rewrite({})


def test_a_callback_needs_a_written_reason_that_it_is_safe_to_run_twice():
    async def reencrypt(tx, chunk, binds):
        return 0

    with pytest.raises(PassDeclarationError) as error:
        Apply(reencrypt)

    message = " ".join(str(error.value).split())
    # Job delivery is at-least-once, so the question cannot be avoided -- only
    # answered. There is deliberately no strict=False.
    assert "at-least-once" in message
    assert "Declared(" in message


def test_the_written_reason_may_not_be_empty():
    async def reencrypt(tx, chunk, binds):
        return 0

    with pytest.raises(PassDeclarationError, match="needs a reason"):
        Apply(reencrypt, idempotent=Declared("   "))


def test_a_declared_callback_is_accepted():
    async def reencrypt(tx, chunk, binds):
        return 0

    work = Apply(reencrypt, idempotent=Declared("rows already re-wrapped are excluded by where"))

    assert work.writes == ()


def test_apply_needs_something_callable():
    with pytest.raises(PassDeclarationError, match="async callable"):
        Apply("not a function", idempotent=Declared("because"))


# --- the declaration's own shape ----------------------------------------------


def test_a_table_name_must_be_a_plain_identifier():
    with pytest.raises(PassDeclarationError, match="plain SQL identifier"):
        Table("replays; DROP TABLE treks")


def test_a_qualified_table_renders_both_parts_quoted():
    assert Table("replays", schema="wreath").sql == '"wreath"."replays"'
    assert Table("replays").sql == "replays"


def test_a_pass_needs_a_name():
    with pytest.raises(PassDeclarationError, match="1..200 characters"):
        declare(name="")


def test_units_must_be_a_range_source():
    with pytest.raises(PassDeclarationError, match="must be a range source"):
        declare(units="the next thousand")


def test_work_must_be_a_declared_shape():
    with pytest.raises(PassDeclarationError, match="must be Purge"):
        declare(work=lambda tx, chunk, binds: None)


def test_a_frontier_must_be_a_ceiling_or_sealed():
    with pytest.raises(PassDeclarationError, match="must be Ceiling.at_launch"):
        declare(frontier="the end")


def test_a_pass_must_use_the_write_workload():
    # The read pools open with default_transaction_read_only, so a pass on one
    # would half-work rather than fail.
    with pytest.raises(PassDeclarationError, match="must use the write workload"):
        declare(workload="read")


def test_a_chunk_limit_must_be_a_positive_int():
    with pytest.raises(PassDeclarationError, match="positive int"):
        Rows(key=(EXPIRES, KEY), limit=0)


def test_a_duty_cycle_has_no_off_position():
    # A walk with no pacing is the failure the policy exists to prevent, so the
    # primitive must not have a state in which it is disabled.
    with pytest.raises(ValueError, match="no pacing is the failure"):
        DutyCycle(0.0)
    with pytest.raises(ValueError, match="no pacing is the failure"):
        DutyCycle(1.5)


def test_a_duty_cycle_rests_in_proportion_to_the_work_it_did():
    # At a quarter of wall time, three seconds of rest per second of work.
    assert DutyCycle(0.25).rest_after(1.0) == pytest.approx(3.0)
    assert DutyCycle(0.5).rest_after(2.0) == pytest.approx(2.0)
    assert DutyCycle(1.0).rest_after(5.0) == pytest.approx(0.0)
    assert DutyCycle(0.25).rest_after(0.0) == 0.0


def test_the_pacing_policy_says_why_it_is_holding_the_pass_back():
    # A paced pass that does not say it is paced is indistinguishable from a
    # broken one, so the reason is written to the ledger rather than inferred.
    assert DutyCycle(0.25).reason == "duty cycle 0.25"


# --- the frontier -------------------------------------------------------------


def test_a_fixed_ceiling_over_an_unordered_key_is_refused_at_declaration():
    with pytest.raises(PassDeclarationError, match="assigned in increasing order"):
        ChunkedPass(
            "normalise_grades",
            over=Table("treks"),
            units=Rows(key=Key("id", "uuid", indexed=True, unique=True), within="2s"),
            frontier=Ceiling.at_launch(),
            work=Rewrite({"grade": "'easy'"}),
        )


def test_a_fixed_ceiling_over_a_monotone_key_is_accepted():
    walk = ChunkedPass(
        "normalise_grades",
        over=Table("treks"),
        units=Rows(key=ID, within="2s"),
        frontier=Ceiling.at_launch(),
        work=Rewrite({"grade": "'easy'"}),
    )

    assert walk.recurring is False


def test_a_ceilings_written_reason_may_not_be_empty():
    with pytest.raises(PassDeclarationError, match="needs a reason"):
        Ceiling.at_launch(monotone="  ")


def test_a_clock_frontier_over_a_non_timestamp_key_is_refused_at_declaration():
    with pytest.raises(PassDeclarationError, match="must be a timestamp"):
        ChunkedPass(
            "purge",
            over=Table("treks"),
            units=Rows(key=ID, within="2s"),
            frontier=Sealed(),
            work=Purge(),
        )


def test_a_sealed_frontier_makes_the_pass_recurring():
    assert declare().recurring is True


def test_a_sealed_frontier_may_be_held_back_from_the_present():
    assert Sealed(after="1h").after == 3600.0
    assert Sealed().after == 0.0


def test_a_sealed_frontier_refuses_a_negative_hold_back():
    # A negative number reaches the sign check; a negative *duration string* is
    # refused one step earlier, by the grammar, which never admits a minus sign.
    with pytest.raises(PassDeclarationError, match="must be positive"):
        Sealed(after=-5)
    with pytest.raises(PassDeclarationError, match="must be a number of seconds"):
        Sealed(after="-5s")


def test_a_sealed_frontier_of_zero_means_everything_already_past():
    # Zero is the one duration that is legal here and nowhere else: an expiry
    # purge wants every row the clock has already passed, with no hold-back.
    assert Sealed(after=0).after == 0.0
