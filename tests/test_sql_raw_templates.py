from __future__ import annotations

import pytest

from wreath.orm.errors import SessionError
from wreath.orm.session import Session

INJECTION = "zz%') UNION SELECT id, label, secret, 'x', 'y' FROM vault --"


class _Unconnected(Session):
    """A session with no pool behind it. `raw()` never reaches one."""

    def __init__(self) -> None:  # pragma: no cover - trivial
        self._closed = False

    def _check_usable(self) -> None:
        return None


def test_raw_compiles_a_template_to_placeholders() -> None:
    session = _Unconnected()
    reference = "NW-0001"
    query = session.raw(t"SELECT id FROM shipments WHERE reference = {reference}")
    assert query._sql == "SELECT id FROM shipments WHERE reference = $1"
    assert query._args == ("NW-0001",)


def test_raw_binds_an_injection_payload_as_data() -> None:
    session = _Unconnected()
    pattern = f"%{INJECTION}%"
    query = session.raw(t"SELECT id FROM shipments WHERE reference ILIKE {pattern}")
    assert "UNION" not in query._sql
    assert query._args == (f"%{INJECTION}%",)


def test_raw_still_takes_text_and_arguments() -> None:
    session = _Unconnected()
    query = session.raw("SELECT id FROM shipments WHERE id = $1", 7)
    assert query._sql == "SELECT id FROM shipments WHERE id = $1"
    assert query._args == (7,)


def test_raw_refuses_a_template_together_with_arguments() -> None:
    session = _Unconnected()
    value = 1
    with pytest.raises(SessionError, match="not both"):
        session.raw(t"SELECT {value}", 2)


def test_raw_refuses_an_empty_template() -> None:
    session = _Unconnected()
    with pytest.raises(SessionError, match="non-empty"):
        session.raw(t"")


def test_raw_refuses_an_empty_string() -> None:
    session = _Unconnected()
    with pytest.raises(SessionError, match="non-empty"):
        session.raw("")
