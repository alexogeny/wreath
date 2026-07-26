# Run a transaction (unit of work)

When several writes must land together or not at all — debit one account, credit
another, record the transfer — wrap them in a transaction. `session.begin()` is an
async context manager that opens the transaction on entry, commits on a clean
exit, and rolls back if anything raises:

```python
from typing import Annotated
from wreath.orm import FromORM, Session

@app.post("/transfers")
async def transfer(
    request,
    body: Transfer,                                          # validated by the model
    session: Annotated[Session, FromORM("main", workload="write")],
) -> dict:
    async with session.begin():
        debit = Entry(account_id=body.from_id, amount=-body.amount)
        credit = Entry(account_id=body.to_id, amount=body.amount)
        session.add(debit)
        session.add(credit)
        await session.flush()                                # INSERTs run; ids populated
    return {"debit": debit.id, "credit": credit.id}
```

`session.add` stages an object; `session.flush()` sends the pending `INSERT`s and
populates server-generated columns like `id`, while the surrounding `begin()`
keeps everything in one `BEGIN … COMMIT`. Raise inside the block — a business-rule
check, a failed constraint — and the whole unit rolls back, never a half-applied
transfer. `begin()` nests: an inner `begin()` becomes a `SAVEPOINT`, so an inner
failure rolls back to its savepoint while the outer transaction carries on. A bare
`flush()` outside `begin()` still runs atomically in its own transaction, which is
all a single-statement write needs.
