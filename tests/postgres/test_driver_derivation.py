from __future__ import annotations

import pytest

import wreath.postgres as pg
from wreath import _pgdriver as pure
from wreath._devtools import query_probe
from wreath._replay_adapters import driver_row_value
from wreath.orm.types import EXT_KIND_VECTOR

native = pytest.importorskip("wreath._native._postgres")

#: A plausible extension-assigned OID, distinct from every other suite's: one
#: OID must never mean two wire formats, and the codec table refuses a second
#: kind for a number it already knows.
PROBE_VECTOR_OID = 987671


def test_a_registered_extension_column_decodes_the_way_the_driver_decodes() -> None:
    pg.register_extension_codec("wreath_probe_vector", PROBE_VECTOR_OID, EXT_KIND_VECTOR)

    assert driver_row_value(PROBE_VECTOR_OID, b"[1,2,3]") == [1.0, 2.0, 3.0]


#: Which module the driver's answers must come from in *this* process. Named by
#: resolution rather than outright, because a build without `_postgres` runs the
#: Python half directly and the contract is the same either way.
_DRIVER = native if pg._implementation == "native" else pure
_OTHER = pure if pg._implementation == "native" else native


def test_the_facade_answers_with_the_driver_that_loaded_not_the_other_one() -> None:
    assert pg._decode_value is _DRIVER._decode_value
    assert pg._decode_value is not _OTHER._decode_value
    assert pg._is_transaction_sql is _DRIVER._is_transaction_sql
    assert pg._is_transaction_sql is not _OTHER._is_transaction_sql


def test_the_probe_prices_the_transaction_test_its_own_submit_runs() -> None:
    assert query_probe._transaction_test(ungrafted=False) is _DRIVER._is_transaction_sql
    assert query_probe._transaction_test(ungrafted=True) is pure._is_transaction_sql


def test_the_native_connection_inherits_the_python_state_machine() -> None:
    assert native.Connection.__mro__[1] is pure.Connection
    assert query_probe._reference_state_machine() is pure

    plan = query_probe._graft_plan(pure.Connection)
    assert [name for name, _original in plan] == list(query_probe.GRAFTED)


def test_a_state_machine_that_has_moved_refuses_the_ab_rather_than_reporting_it() -> None:
    with pytest.raises(SystemExit, match=r"compare the native pipeline against itself"):
        query_probe._graft_plan(object)
