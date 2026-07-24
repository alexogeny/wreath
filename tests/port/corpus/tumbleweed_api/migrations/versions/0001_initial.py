"""initial tumbleweed schema

Revision ID: 0001_initial
Revises:

Exercises the Alembic surface the codemod does NOT translate (keeps in Alembic):
``create_table``, ``add_column`` with a ``server_default``, and an
``alter_column(... postgresql_using=...)`` cast (the MANUAL case).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ranch",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False, unique=True),
        sa.Column("settings", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "llama",
        sa.Column("pack_weight_kg", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column(
        "booking",
        "guests",
        type_=sa.Integer(),
        postgresql_using="guests::integer",
    )


def downgrade() -> None:
    op.drop_column("llama", "pack_weight_kg")
    op.drop_table("ranch")
