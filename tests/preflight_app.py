from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from wreath.app import Wreath
from wreath.config import Env
from wreath.request import Request


@dataclass(frozen=True, slots=True)
class Settings:
    """A key no supplier offers, so `--settings` has something to report."""

    token: Annotated[str, Env("WREATH_PREFLIGHT_FIXTURE_TOKEN")]


app = Wreath()


@app.get("/health")
async def health(request: Request) -> dict:
    return {"ok": True}
