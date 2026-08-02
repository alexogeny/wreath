"""Session write scheduling accepts ORM model instances only."""

from __future__ import annotations

import pytest

from wreath.orm.registry import Registry
from wreath.orm.session import Session


@pytest.mark.parametrize("operation", ["add", "delete"])
def test_write_operation_refuses_a_non_model_with_the_boundary_error(
    registry: Registry, operation: str
) -> None:
    session = Session(registry, "write")

    with pytest.raises(TypeError) as excinfo:
        getattr(session, operation)({"id": 7})

    assert str(excinfo.value) == "expected a model instance, got {'id': 7}"
