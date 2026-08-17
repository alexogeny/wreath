"""Declarative SQL for Wreath's leased-and-fenced work state machine.

Jobs, durable messages, and webhook deliveries have different payloads and
terminal states. They do not have different claiming semantics: lock a bounded
candidate set without waiting, advance the fence in the same update, and make
later transitions name the fence they observed. Domain modules provide only
their columns and predicates.
"""

from __future__ import annotations


def claim_sql(
    table: str,
    *,
    key: str,
    alias: str,
    predicate: str,
    order: str,
    limit: str,
    assignments: str,
    returning: str,
    candidate: str = "claimable",
) -> str:
    """Compile one atomic `SKIP LOCKED` claim and fenced update."""
    alias_name = alias.rsplit(maxsplit=1)[-1]
    return (
        f"WITH {candidate} AS ( SELECT {key} FROM {table} WHERE {predicate} "
        f"ORDER BY {order} FOR UPDATE SKIP LOCKED LIMIT {limit} ) "
        f"UPDATE {table} {alias} SET {assignments} FROM {candidate} c "
        f"WHERE {alias_name}.{key}=c.{key} RETURNING {returning}"
    )


def fenced_update_sql(
    table: str,
    assignments: str,
    *,
    key: str = "id",
    fence: str = "fence",
    state: str | None = None,
) -> str:
    """Compile a transition that a stale worker cannot apply."""
    condition = f"WHERE {key}=$1 AND {fence}=$2"
    if state is not None:
        condition += f" AND state={state!r}"
    return f"UPDATE {table} SET {assignments} {condition}"


__all__: tuple[str, ...] = ()
