"""A second module defining a same-named model, to exercise name collisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Item:
    """A different ``Item`` than the one in :mod:`tests.typegen.app`."""

    sku: str
    quantity: int
