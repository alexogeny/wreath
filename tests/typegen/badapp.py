"""An application whose annotations are unsupported under strict typegen."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from wreath import Wreath


@dataclass
class Token:
    id: uuid.UUID


app = Wreath()


@app.get("/tokens/{token_id}")
async def get_token(request, token_id: int) -> Token: ...
