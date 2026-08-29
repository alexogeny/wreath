from __future__ import annotations

from typing import Annotated

from wreath import Request, Router
from wreath.orm import FromORM, Session
from wreath.sql import Fragment, Identifier

ReadSession = Annotated[Session, FromORM("main", workload="read")]
freight = Router(prefix="/shipments")

SCHEMA = "northwind"
DIRECTIONS = {"asc": "ASC", "desc": "DESC"}


@freight.get("/search")
async def search(request: Request, db: ReadSession, q: str = "") -> dict:
    org = request.state.session["principal"]["org_id"]
    pattern = f"%{q}%"
    rows = await db.raw(
        t"SELECT id, reference, origin FROM {Identifier(SCHEMA, 'shipments')} "
        t"WHERE org_id = {org} AND reference ILIKE {pattern} "
        t"ORDER BY 1 LIMIT 25"
    ).fetch()
    columns = ("id", "reference", "origin")
    return {"matches": [dict(zip(columns, tuple(r), strict=True)) for r in rows]}


@freight.get("/by-status")
async def by_status(db: ReadSession, status: str = "booked", order: str = "asc") -> dict:
    # An ORDER BY direction is syntax, not data, so it cannot be bound. It is
    # looked up in a mapping this module owns, which turns a hostile value into
    # a KeyError rather than into SQL.
    direction = Fragment(DIRECTIONS[order])
    rows = await db.raw(
        t"SELECT id FROM shipments WHERE status = {status} ORDER BY id {direction}"
    ).fetch()
    return {"ids": [r[0] for r in rows]}


@freight.get("/reported")
async def reported(db: ReadSession) -> dict:
    # Static SQL with no interpolation at all stays a plain string.
    rows = await db.raw("SELECT id FROM shipments WHERE status = 'reported'").fetch()
    return {"ids": [r[0] for r in rows]}


@freight.get("/paged")
async def paged(db: ReadSession, after: int = 0) -> dict:
    # Placeholders written by hand are still correct, and still the fastest
    # thing to write for a statement with no composition in it.
    rows = await db.raw("SELECT id FROM shipments WHERE id > $1 LIMIT 20", after).fetch()
    return {"ids": [r[0] for r in rows]}
