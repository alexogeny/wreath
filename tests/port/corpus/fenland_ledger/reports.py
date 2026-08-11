"""Month-end reporting, which grew out of the query module and still takes it
wholesale.

``import *`` is why nothing below can be attributed with confidence: the names
this module calls may come from ``queries``, from something ``queries`` itself
imported, or from a builtin of the same name.
"""
from datetime import date

from .queries import *


async def month_end(slug: str) -> dict:
    ledger = await ledger_by_slug_or_currency(slug)
    latest = await newest_entry()
    return {
        "ledger": ledger.slug,
        "closed_on": date.today().isoformat(),
        "latest_entry": latest.id if latest else None,
    }
