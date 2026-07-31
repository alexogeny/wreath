"""The purge pass a :mod:`wreath.store` declaration implies.

Three of Wreath's own tables -- idempotency replays, rate-limit buckets, server
side sessions -- are keyed stores that need expired rows dropped forever. That is
a recurring pass with no gate, over exactly the tables that get large in the
deployments where getting it wrong matters, and writing it once here is the same
argument :mod:`wreath.store` itself makes about the six disciplines its three
callers used to re-derive.

The key is ``(stamp, key)``: the stamp because that is the ordered domain the
frontier is measured in, and the primary key appended as a tiebreaker because a
stamp is not unique and a boundary value that is not unique either skips rows or
loops on them forever.
"""

from __future__ import annotations

from typing import Any


def keyed_purge_pass(
    declaration: Any,
    *,
    name: str,
    after: float = 0.0,
    chunk: int = 1000,
    within: Any = "5s",
    shift: Any = "10s",
    pace: Any = None,
    schema: str = "wreath",
    tenant: str = "",
) -> Any:
    """A recurring :class:`~wreath.passes.ChunkedPass` that drops this store's dead rows.

    Takes no database. A :class:`~wreath.passes.ChunkedPass` is a declaration --
    it is handed the database when it is *driven*, by
    :meth:`~wreath.passes.ChunkedPass.run` or by the scheduler -- so a connection
    passed at build time had nowhere to go and was silently discarded by all
    three callers.
    """
    from ..passes import (
        ChunkedPass,
        Key,
        PassDeclarationError,
        Purge,
        Rows,
        Sealed,
        Table,
    )

    if not declaration.index_stamp:
        # The keyset refusal would catch this anyway, but it would say "declare
        # an index", and for a store declaration the fix has a name.
        raise PassDeclarationError(
            f"store {declaration.table!r} purges by {declaration.stamp!r} but was "
            f"declared without index_stamp=True, so every chunk would sort the "
            "whole table -- worse than the unbounded DELETE a pass replaces. "
            "Declare Keyed(..., index_stamp=True) and migrate the index in."
        )
    keys = (
        Key(declaration.stamp, "timestamptz", indexed=True),
        Key(declaration.key, "text", unique=True),
    )
    return ChunkedPass(
        name,
        over=Table(declaration.table),
        units=Rows(key=keys, limit=chunk, within=within),
        frontier=Sealed(after=after),
        work=Purge(),
        pace=pace,
        # An expiry purge has no terminal step, so there is no irreversible
        # thing a skip could buy: one undeletable row must not stop the table
        # from being kept small forever. The hole is still recorded, and
        # `wreath passes retry` still comes back for it.
        on_chunk_failure="skip",
        shift=shift,
        schema=schema,
        tenant=tenant,
    )


__all__ = ["keyed_purge_pass"]
