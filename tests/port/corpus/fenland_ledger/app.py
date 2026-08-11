"""The ledger service. The routes are thin; the reads live in ``queries``."""
from datetime import date

from fastapi import Depends, FastAPI

from .queries import ensure_ledger, entry_page
from .schemas import EntryDraft
from .storage import get_receipt_store

app = FastAPI(title="Fenland Ledger")


@app.get("/entries")
async def entries(page: int = 1):
    return await entry_page(page)


@app.post("/entries", status_code=201)
async def post_entry(draft: EntryDraft, receipts=Depends(get_receipt_store)):
    ledger, _ = await ensure_ledger(draft.ledger_slug, date.today())
    receipts.put(f"{ledger.slug}/{date.today():%Y-%m}", b"")
    return {"ledger": ledger.slug, "amount_minor": draft.amount_minor}
