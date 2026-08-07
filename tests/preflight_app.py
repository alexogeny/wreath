"""A target for `wreath doctor preflight`, loaded through the CLI by name.

Deliberately ordinary: one public route and one settings model whose key nothing
supplies. The CLI has to import a real module path, so this cannot be built
inside the test that uses it.
"""

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
