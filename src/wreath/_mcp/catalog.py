"""One bounded, name-addressed collection with its listing cached as bytes.

Tools, resources and prompts are three different things a model reads and one
identical piece of bookkeeping: a name must be unique, the collection must have
a ceiling, and the `*/list` payload should be serialized once rather than per
call. That last part is the only optimization `wreath.mcp` makes and it is the
same trade the router makes -- compile at startup, spend nothing at request
time. The cache is invalidated by registration rather than by a timer, so
declaring an entry after the first listing was served is correct rather than
merely tolerated.

The ceilings are not defensive programming. Every entry is text a model reads
*before every decision it makes*, so a large list makes each of those decisions
worse; refusing at registration puts that trade in front of whoever is writing
the declaration, which is the only moment anyone can act on it.
"""

from __future__ import annotations

from typing import Any

from .._json import dumps as _json_dumps


class Catalog:
    """A bounded `name -> entry` map that renders one `*/list` result.

    Subclasses name the noun, the ceiling that bounds them, and the key the
    listing is rendered under. Everything else is shared, because it is.
    """

    __slots__ = ("_listing", "entries", "max_entries")

    #: What one entry is called, in a refusal a human reads.
    noun = "entry"
    #: The `MCPLimits` field that bounds this catalog, named in the refusal so
    #: the remedy is in the message rather than in the reference page.
    ceiling = "max_entries"
    #: The key the listing renders under, e.g. `tools` for `{"tools": [...]}`.
    listing_key = "entries"

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max_entries
        self.entries: dict[str, Any] = {}
        self._listing: bytes | None = None

    def __len__(self) -> int:
        return len(self.entries)

    def insert(self, key: str, entry: Any) -> None:
        """Register `entry` under `key`.

        Raises:
            ValueError: The key is taken, or the catalog is at its ceiling.
        """
        if key in self.entries:
            raise ValueError(
                f"{self.noun} {key!r} is already registered. A name is how a "
                "model addresses it, so two of them is an ambiguity no caller "
                "can resolve."
            )
        if len(self.entries) >= self.max_entries:
            raise ValueError(
                f"this MCP server already declares {self.max_entries} "
                f"{self.noun}s, its `MCPLimits({self.ceiling}=...)` ceiling. "
                "Every one of them is text a model reads before every decision, "
                "so a long list makes each of those decisions worse; split the "
                "server, or raise the ceiling knowing that is the trade."
            )
        self.entries[key] = entry
        self._listing = None

    def get(self, key: str) -> Any:
        return self.entries.get(key)

    def sorted_entries(self) -> list[Any]:
        """Every entry, in the order a listing renders them."""
        return [self.entries[key] for key in sorted(self.entries)]

    def listing(self) -> bytes:
        """The serialized `*/list` result, computed once and reused.

        Sorted, so two processes serving the same declarations render the same
        bytes and a client diffing a listing sees a real change or none.
        """
        cached = self._listing
        if cached is None:
            cached = self._listing = _json_dumps(
                {self.listing_key: [entry.describe() for entry in self.sorted_entries()]}
            )
        return cached


__all__ = ["Catalog"]
