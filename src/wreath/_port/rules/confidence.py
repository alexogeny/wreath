"""How certain the analysis itself is about what it read."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW

CONFIDENCE: dict[str, tuple[str, str, str, str]] = {
    # -- confidence -----------------------------------------------------------
    "resolve.star_import": (
        "star_import",
        "other",
        NEEDS_REVIEW,
        "This module uses `from ... import *`, so this tool cannot always tell where a name came from. Anything it reported here is less certain than usual; the quickest fix is to import the names you use.",
    ),
}
