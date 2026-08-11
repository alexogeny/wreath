"""What a caller may post.

The validator is a rule about a value, not a restatement of the annotation, so
there is nothing in the type for wreath's binding to enforce in its place.
"""
from pydantic import BaseModel, field_validator


class EntryDraft(BaseModel):
    ledger_slug: str
    amount_minor: int
    memo: str | None = None

    @field_validator("amount_minor")
    @classmethod
    def reject_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("an entry of zero moves no money")
        return value
