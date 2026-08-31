from wreath.orm import Mapped, Model, column
from wreath.orm.compiler import compile_update_many
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Vector

from .conftest import FakeDatabase


class VectorRow(Model, table="unnest_vector_rows"):
    id: Mapped[int] = column(Int64, primary_key=True)
    embedding: Mapped[list[float]] = column(Vector(3))


def test_update_batch_falls_back_when_postgresql_has_no_array_codec() -> None:
    registry = Registry(
        FakeDatabase(),
        [VectorRow],
        validate_schema="off",
    )
    spec = registry.spec_for(VectorRow)

    two = compile_update_many(registry, spec, 1 << 1, 2)
    three = compile_update_many(registry, spec, 1 << 1, 3)

    assert two.array_oids == ()
    assert "FROM (VALUES " in two.sql
    assert two is not three
