import pytest

from wreath.state import State


def test_state_attribute_lifecycle_and_require() -> None:
    state = State()
    state.database = "db"

    assert state.database == "db"
    assert state.get("database") == "db"
    assert state.require("database") == "db"

    del state.database
    with pytest.raises(AttributeError):
        _ = state.database
    with pytest.raises(RuntimeError, match="database"):
        state.require("database")
