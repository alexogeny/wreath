from __future__ import annotations

import importlib
from typing import Annotated, Any

from wreath import Request, Router
from wreath.exceptions import Forbidden, NotFound
from wreath.orm import FromORM, Session

from .models import Account  # type: ignore[import-not-found]

WriteSession = Annotated[Session, FromORM("main", workload="write")]
automation = Router(prefix="/automation")
accounts = Router(prefix="/accounts")

OPS_ALLOWLIST = {"OPS@NORTHWIND.EXAMPLE", "SRE@NORTHWIND.EXAMPLE"}


@automation.post("/run")
async def run_action(request: Request) -> dict:
    payload = await request.json()
    action = str(payload["action"])
    parts = action.split(".")
    module = importlib.import_module(".".join(parts[:-1]))  # hardening-expect: dynamic-import
    target = getattr(module, parts[-1], None)
    if target is None:
        raise NotFound(f"no automation action {action!r}")
    return {"result": target()}


@accounts.patch("/me")
async def update_me(request: Request, db: WriteSession) -> dict:
    payload = await request.json()
    caller = request.state.session["principal"]["id"]
    account = await db.fetch_one(Account.select().where(Account.id == caller))
    changed = []
    for key, value in payload.items():
        if key.startswith("_") or key == "id":
            continue
        if hasattr(account, key):
            setattr(account, key, value)  # hardening-expect: mass-assignment
            changed.append(key)
    await db.flush()
    return {"updated": changed}


@accounts.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    email = request.state.session["principal"]["email"]
    if email.upper() not in OPS_ALLOWLIST:  # hardening-expect: case-mapped-authz
        raise Forbidden("this dashboard is for operations staff")
    return {"region": "eu-west-1"}
