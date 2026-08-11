"""The suite swaps the receipt store for one that keeps its writes in memory.

That is an adapter, not an identity: nothing here is pretending to be a signed-in
caller, so the replacement is about where bytes go and not about who is asking.
"""
from fastapi.testclient import TestClient

from .app import app
from .storage import get_receipt_store


class InMemoryReceiptStore:
    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    def put(self, key: str, blob: bytes) -> None:
        self.written[key] = blob


def test_posting_an_entry_writes_a_receipt():
    app.dependency_overrides[get_receipt_store] = InMemoryReceiptStore
    client = TestClient(app)

    response = client.post(
        "/entries", json={"ledger_slug": "fen-north", "amount_minor": 2500}
    )

    assert response.status_code == 201
    assert response.json()["ledger"] == "fen-north"
