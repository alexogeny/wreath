"""An Alembic revision mixing three kinds of operation.

The mix is the point. Ordinary DDL is what ``wreath migrations generate``
derives from a model change; ``op.execute`` is raw SQL no generator can infer;
and ``op.get_bind()`` marks the one that rewrites *rows* — the revision that
takes an hour on a large table and holds up a deploy.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_grade_bands"
down_revision = "0001_initial"


def upgrade() -> None:
    op.add_column("llama", sa.Column("band", sa.String(length=16), nullable=True))
    op.create_index("ix_llama_band", "llama", ["band"])
    op.alter_column("llama", "fleece_kg", nullable=True)

    # The data migration: every existing row is rewritten.
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, grade FROM llama")).fetchall()
    for row in rows:
        band = "prime" if row.grade >= 4 else "standard"
        connection.execute(
            sa.text("UPDATE llama SET band = :band WHERE id = :id"),
            {"band": band, "id": row.id},
        )

    op.execute("ANALYZE llama")


def downgrade() -> None:
    op.drop_index("ix_llama_band", table_name="llama")
    op.drop_column("llama", "band")
