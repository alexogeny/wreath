"""Reads whose *arguments* stop them carrying across.

Every verb here has a wreath spelling. What each one is waiting on is written
in its own line: a lookup through a relation whose target model is not in this
tree, a positional ``Q`` predicate, a ``first()`` with nothing to order by, a
``get_or_create`` whose defaults are computed, and a page with no size.
"""
from datetime import date

import ormar

from .models import Entry, Ledger


async def entries_for_custodian(reference: str) -> list[Entry]:
    return await Entry.objects.filter(custodian__reference=reference).all()


async def ledger_by_slug_or_currency(term: str) -> Ledger:
    return await Ledger.objects.get(ormar.or_(slug=term, currency=term))


async def newest_entry() -> Entry | None:
    return await Entry.objects.first()


async def ensure_ledger(slug: str, opened_on: date) -> tuple[Ledger, bool]:
    return await Ledger.objects.get_or_create(
        slug=slug, _defaults={"currency": _house_currency(slug), "opened_on": opened_on}
    )


async def entry_page(page: int) -> list[Entry]:
    return await Entry.objects.paginate(page).all()


def _house_currency(slug: str) -> str:
    return "EUR" if slug.startswith("eu-") else "GBP"
