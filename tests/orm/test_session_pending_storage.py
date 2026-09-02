from __future__ import annotations

from types import SimpleNamespace

from wreath.orm.session import Session


def test_deleted_queue_allocates_only_when_the_first_item_is_scheduled() -> None:
    registry = SimpleNamespace(schema_mode=None, statement_timeout=None)
    session = Session(registry, "read")

    empty = session._deleted
    assert isinstance(empty, tuple)

    marker = object()
    session._schedule_deleted(marker)
    assert session._deleted == [marker]
    assert isinstance(session._deleted, list)

    session._clear_pending()
    assert session._deleted is empty
