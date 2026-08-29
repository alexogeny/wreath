from __future__ import annotations

from typing import Annotated

from wreath import Request, Router
from wreath.exceptions import Forbidden
from wreath.orm import FromORM, Session

ReadSession = Annotated[Session, FromORM("main", workload="read")]
freight = Router(prefix="/shipments")

SCHEMA = "northwind"


@freight.get("/search")
async def search(request: Request, db: ReadSession, q: str = "") -> dict:
    org = request.state.session["principal"]["org_id"]
    if "drop" in q.lower():
        raise Forbidden("that search term is not allowed")
    needle = q.replace(";", "")
    sql = (
        f"SELECT id, reference, origin FROM {SCHEMA}.shipments "
        f"WHERE org_id = {org} AND reference ILIKE '%{needle}%' "
        f"ORDER BY 1 LIMIT 25"
    )
    rows = await db.raw(sql).fetch()  # hardening-expect: sql-interpolation
    columns = ("id", "reference", "origin")
    return {"matches": [dict(zip(columns, tuple(r), strict=True)) for r in rows]}


@freight.get("/by-status")
async def by_status(db: ReadSession, status: str = "booked") -> dict:
    # The same defect without the intermediate variable, and with `%` rather
    # than an f-string. One rule, three spellings.
    query = "SELECT id FROM shipments WHERE status = '%s'" % status
    rows = await db.raw(query).fetch()  # hardening-expect: sql-interpolation
    return {"ids": [r[0] for r in rows]}


@freight.get("/by-reference")
async def by_reference(db: ReadSession, reference: str = "") -> dict:
    rows = await db.raw(  # hardening-expect: sql-interpolation
        "SELECT id FROM shipments WHERE reference = '" + reference + "'"
    ).fetch()
    return {"ids": [r[0] for r in rows]}


@freight.get("/by-origin")
async def by_origin(db: ReadSession, origin: str = "") -> dict:
    rows = await db.raw(  # hardening-expect: sql-interpolation
        "SELECT id FROM shipments WHERE origin = '{}'".format(origin)
    ).fetch()
    return {"ids": [r[0] for r in rows]}
