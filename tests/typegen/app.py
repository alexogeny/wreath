from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal

from wreath import Wreath

from .other import Item as OtherItem


class Priority(enum.Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class Tag:
    name: str
    color: str | None = None


@dataclass
class Item:
    name: str
    price: float
    priority: Priority
    kind: Literal["basic", "premium"]
    tags: list[Tag] = field(default_factory=list)
    coordinates: tuple[float, float] | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    parent: Item | None = None


@dataclass
class ItemPage:
    items: list[Item]
    total: int


def build_app() -> Wreath:
    app = Wreath()

    @app.get("/items", tags=("items",))
    async def list_items(request, limit: int = 20, cursor: str | None = None) -> ItemPage: ...

    @app.get("/items/{item_id}", operation_id="getItem", tags=("items",))
    async def get_item(request, item_id: int, expand: bool = False, trace_id: str = "") -> Item: ...

    @app.post("/items", operation_id="createItem", tags=("items",))
    async def create_item(request, item: Item) -> Item: ...

    @app.delete("/items/{item_id}", tags=("items",))
    async def delete_item(request, item_id: int) -> None: ...

    @app.get("/inventory/{sku}", tags=("inventory",))
    async def get_inventory(request, sku: str) -> OtherItem: ...

    return app


app = build_app()
