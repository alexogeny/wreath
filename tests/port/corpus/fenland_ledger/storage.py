"""Where signed receipts land.

One accessor, so the routes depend on the function rather than on the store —
which is also the seam the suite reaches through.
"""


class ReceiptStore:
    def __init__(self, root: str) -> None:
        self.root = root

    def put(self, key: str, blob: bytes) -> None:
        raise NotImplementedError("the object store is configured per deployment")


def get_receipt_store() -> ReceiptStore:
    return ReceiptStore(root="/var/lib/fenland/receipts")
