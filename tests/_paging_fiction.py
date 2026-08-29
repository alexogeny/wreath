from __future__ import annotations

from typing import Annotated

from wreath.binding import Query


def legacy_page_params(
    page: Annotated[int, Query(minimum=1, maximum=10_000)] = 1,
    size: Annotated[int, Query(minimum=1, maximum=100)] = 20,
    sort: Annotated[str, Query()] = "",
) -> tuple[int, int, str]:
    """The exact shape that shipped. Never call it; it exists to be read."""
    return (page, size, sort)
