"""The first caller: the five purge loops a chunked pass replaces.

Three of Wreath's own store tables were a single unbounded ``DELETE`` -- the long
transaction one transaction per chunk exists to prevent -- and the two webhook
purges had a chunk size with no cursor, no resumption, and no pacing. That is
what a primitive looks like while it is being rediscovered, so retiring all five
onto one is the point of doing this caller first.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.passes import ChunkedPass, PassDeclarationError
from wreath.store import Column, Keyed

from .conftest import NOW
from .fakes import FakeDatabase, World

pytestmark = pytest.mark.anyio


REPLAYS = Keyed(
    table="wreath_idempotency",
    columns=(Column("response", "jsonb", null=True),),
    key="key",
    stamp="expires",
    index_stamp=True,
)


async def _nap(_seconds):
    return None


def _purge(declaration=REPLAYS, **options):
    from wreath._passes.stores import keyed_purge_pass

    return keyed_purge_pass(declaration, name="purge_replays", **options)


# --- the declaration a store implies ------------------------------------------


def test_a_keyed_store_yields_a_recurring_pass_over_its_own_table():
    walk = _purge()

    assert isinstance(walk, ChunkedPass)
    assert walk.table == "wreath_idempotency"
    # An expiry purge never finishes -- rows keep expiring -- so it recurs.
    assert walk.recurring is True


def test_the_purge_key_appends_the_primary_key_as_a_tiebreaker():
    walk = _purge()

    # The stamp alone is not unique, and a boundary value that is not unique
    # either skips its siblings or loops on them forever.
    assert [key.name for key in walk.units.keys] == ["expires", "key"]
    assert walk.units.keys[0].indexed is True
    assert walk.units.keys[1].unique is True


def test_a_store_with_no_index_on_its_stamp_is_refused_by_name():
    unindexed = Keyed(table="sessions", key="key", stamp="expires", index_stamp=False)

    with pytest.raises(PassDeclarationError) as error:
        _purge(unindexed)

    message = " ".join(str(error.value).split())
    # The generic keyset refusal would say "declare an index"; for a store
    # declaration the fix has a name, so the message uses it.
    assert "index_stamp=True" in message
    assert "worse than the unbounded DELETE" in message


def test_the_purge_is_paced_by_default():
    # There is no unpaced release of this: a walk that goes as fast as it can is
    # the failure the policy exists to prevent.
    assert _purge().pace.fraction == 0.25


def test_an_idle_store_purge_holds_the_frontier_back():
    # A rate-limit bucket is aged by last touch rather than by a deadline, so
    # the frontier sits an hour behind the clock.
    walk = _purge(after=3600.0)

    assert walk.frontier.after == 3600.0


# --- the walk over a real store table -----------------------------------------


async def test_the_purge_drops_expired_rows_and_leaves_live_ones():
    rows = [
        {"key": f"k{index}", "expires": NOW - datetime.timedelta(seconds=50 - index)}
        for index in range(6)
    ]
    rows += [{"key": "fresh", "expires": NOW + datetime.timedelta(seconds=300)}]
    world = World("wreath_idempotency", rows)
    walk = _purge(chunk=2)

    result = await walk.run(FakeDatabase(world), sleep=_nap)

    assert result.complete is True
    assert result.rows == 6
    assert [row["key"] for row in world.rows] == ["fresh"]


async def test_the_purge_walks_in_chunks_rather_than_one_delete():
    world = World(
        "wreath_idempotency",
        [
            {"key": f"k{index}", "expires": NOW - datetime.timedelta(seconds=50 - index)}
            for index in range(6)
        ],
    )

    result = await _purge(chunk=2).run(FakeDatabase(world), sleep=_nap)

    # Six rows in chunks of two: three transactions, not one held open for the
    # length of the whole delete.
    assert result.chunks == 3
    assert len([sql for sql, _ in world.statements if sql == "BEGIN"]) == 3


async def test_a_redeploy_mid_purge_resumes_rather_than_restarting():
    world = World(
        "wreath_idempotency",
        [
            {"key": f"k{index:02d}", "expires": NOW - datetime.timedelta(seconds=50 - index)}
            for index in range(6)
        ],
    )
    database = FakeDatabase(world)
    walk = _purge(chunk=2)

    # One shift's worth, then the process goes away.
    first = await walk.run_shift(database, budget=0.0, sleep=_nap)
    assert first.chunks == 0

    import asyncio

    stopping = asyncio.Event()
    seen = {"chunks": 0}

    def stop_after_one(sql, args):
        if sql.startswith("DELETE FROM wreath_idempotency"):
            seen["chunks"] += 1
            if seen["chunks"] == 1:
                stopping.set()

    world.before = stop_after_one
    stopped = await walk.run_shift(database, stopping=stopping, sleep=_nap)
    assert stopped.stopped == "stopping"
    assert stopped.chunks == 1
    cursor_after_stop = (await walk.status(database)).cursor

    # A fresh process picks up from the ledger, not from the top of the index.
    world.before = None
    resumed = await walk.run(database, sleep=_nap)

    assert cursor_after_stop is not None
    assert resumed.rows == 4  # the two already deleted are not deleted again
    assert world.rows == []


# --- the store surfaces -------------------------------------------------------


def test_every_store_that_purges_offers_a_pass():
    from wreath.middleware.idempotency import PostgresIdempotencyStore
    from wreath.middleware.ratelimit import PostgresRateLimitStore
    from wreath.session_store import PostgresSessionStore

    # All three used to be one unbounded DELETE. The unbounded form is kept for
    # a small table and for tests, but the supported route is the pass.
    for store in (PostgresIdempotencyStore, PostgresRateLimitStore, PostgresSessionStore):
        assert hasattr(store, "purge_pass")
        assert hasattr(store, "purge")


def test_the_webhook_inbox_and_outbox_offer_a_pass():
    from wreath.webhooks import PostgresWebhookInbox, PostgresWebhookOutbox

    # These two already had a chunk size -- but no cursor, so they started from
    # the beginning of the index every time, and no pacing.
    for store in (PostgresWebhookInbox, PostgresWebhookOutbox):
        assert hasattr(store, "purge_pass")


def test_the_unbounded_purge_documents_what_it_costs():
    from wreath.middleware.idempotency import PostgresIdempotencyStore

    doc = PostgresIdempotencyStore.purge.__doc__
    assert "one unbounded statement" in doc.lower()
    assert "purge_pass" in doc


def test_a_purge_pass_builder_takes_no_database():
    """A pass is a declaration; it is handed a database when it is driven.

    All three keyed stores passed one and it was discarded, so the signature
    promised a wiring step that did not exist.
    """
    import inspect

    from wreath._passes.stores import keyed_purge_pass

    parameters = inspect.signature(keyed_purge_pass).parameters
    assert "database" not in parameters
    assert [
        name
        for name, p in parameters.items()
        if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ] == ["declaration"]
