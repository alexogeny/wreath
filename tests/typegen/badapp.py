from __future__ import annotations

from dataclasses import dataclass

from wreath import Wreath


@dataclass
class Token:
    id: complex


app = Wreath()


@app.get("/tokens/{token_id}")
async def get_token(request, token_id: int) -> Token: ...
