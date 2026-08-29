from __future__ import annotations

import pytest


@pytest.fixture
def account_model() -> type:
    """A model with one of each shape the admin has to render.

    `password_hash` is here because every view must withhold it without being
    told to -- that is `wreath.crud`'s decision, inherited rather than remade.
    """
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Bool, Int64, Text

    class Account(Model, table="admin_accounts"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        email: Mapped[str] = column(Text)
        note: Mapped[str] = column(Text, nullable=True)
        active: Mapped[bool] = column(Bool, nullable=True)
        password_hash: Mapped[str] = column(Text, nullable=True)

    return Account
