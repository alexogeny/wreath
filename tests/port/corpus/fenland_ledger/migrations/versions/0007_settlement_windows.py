"""Settlement windows — a table the models cannot describe.

``opens_at`` is a time of day and ``grace`` an interval; neither has a column
type in wreath's ORM, so nothing on a model could produce this table. The check
constraint and the constraint drop have no model form either, and the drop does
not even say what kind of constraint it is removing.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_settlement_windows"
down_revision = "0006_entry_memo"


def upgrade() -> None:
    op.create_table(
        "settlement_window",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ledger_slug", sa.String(length=64), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=False),
        sa.Column("grace", sa.Interval(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_check_constraint(
        "ck_settlement_window_grace",
        "settlement_window",
        "grace is null or grace >= interval '0'",
    )
    op.drop_constraint("uq_entry_memo", "entry")


def downgrade() -> None:
    op.drop_table("settlement_window")
