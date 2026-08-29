from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from wreath import Request, Router
from wreath.exceptions import Forbidden, NotFound
from wreath.orm import FromORM, Session

from .models import Account  # type: ignore[import-not-found]

WriteSession = Annotated[Session, FromORM("main", workload="write")]
automation = Router(prefix="/automation")
accounts = Router(prefix="/accounts")

OPS_ALLOWLIST = frozenset({"ops@northwind.example", "sre@northwind.example"})


def _reindex() -> str:
    return "reindexed"


def _resweep() -> str:
    return "reswept"


ACTIONS = {"reindex": _reindex, "resweep": _resweep}


@dataclass(kw_only=True)
class ProfileUpdate:
    """The columns a caller owns. Everything else is not in this model."""

    display_name: str
    timezone: str


@automation.post("/run")
async def run_action(request: Request) -> dict:
    payload = await request.json()
    action = ACTIONS.get(str(payload["action"]))
    if action is None:
        raise NotFound("no such automation action")
    return {"result": action()}


@accounts.patch("/me")
async def update_me(request: Request, db: WriteSession, update: ProfileUpdate) -> dict:
    caller = request.state.session["principal"]["id"]
    account = await db.fetch_one(Account.select().where(Account.id == caller))
    account.display_name = update.display_name
    account.timezone = update.timezone
    await db.flush()
    return {"updated": ["display_name", "timezone"]}


@accounts.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    # Normalised once, when the account was written, and compared as stored.
    # Case-mapping *here* would mean the two sides can disagree about what the
    # same address is, and only one of them decides who gets in.
    email = request.state.session["principal"]["normalised_email"]
    if email not in OPS_ALLOWLIST:
        raise Forbidden("this dashboard is for operations staff")
    return {"region": "eu-west-1"}
