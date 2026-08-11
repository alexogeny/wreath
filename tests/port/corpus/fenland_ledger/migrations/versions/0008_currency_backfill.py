"""Every existing ledger gets an explicit currency, then the column is narrowed.

The mapping from the old blank-and-lowercase spellings to ISO codes is the part
of this revision nothing can derive: it is a decision about historical data, and
it is the reason the table is rewritten row by row before the column can stop
being nullable.
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_currency_backfill"
down_revision = "0007_settlement_windows"

_LEGACY_CURRENCY = {"": "GBP", "gbp": "GBP", "sterling": "GBP", "eur": "EUR"}


def upgrade() -> None:
    connection = op.get_bind()
    for old, new in _LEGACY_CURRENCY.items():
        connection.execute(
            sa.text("UPDATE ledger SET currency = :new WHERE currency = :old"),
            {"new": new, "old": old},
        )

    op.execute("CREATE UNIQUE INDEX ix_ledger_slug ON ledger (lower(slug))")
    op.alter_column(
        "ledger", "currency", nullable=False, existing_type=sa.String(length=3)
    )


def downgrade() -> None:
    op.alter_column(
        "ledger", "currency", nullable=True, existing_type=sa.String(length=3)
    )
    op.drop_index("ix_ledger_slug", table_name="ledger")
