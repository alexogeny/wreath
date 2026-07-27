"""A frozen replica of the `page_params` signature that shipped broken.

`wreath.pagination.page_params` used to be
`page_params(page, size, sort)` with `Query()` markers on its own parameters --
a dependency is called `fn(request, **nested_depends)`, so the request object
arrived *as* the page number and the first comparison against it was a 500.

That is now fixed at the source: `page_params` takes the request and reads the
query string, and route compilation refuses a marker on any dependency
parameter. Which left the docs floor's acceptance test with nothing to check --
its fixture imported the shipped function precisely *because* it exhibited the
defect, so fixing the defect quietly disarmed the test that proves the rule
works.

So the fiction is frozen here instead of borrowed from shipping code. A check
built to catch a known bug has to keep catching it after the bug is gone; see
`docs/decisions/0024-name-the-failure-a-check-that-silently-has-nothing-to-check.md`.

Nothing imports this outside `tests/test_docs_codeblocks.py`.
"""

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
