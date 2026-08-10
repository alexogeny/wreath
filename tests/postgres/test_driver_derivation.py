"""The doubles and probes must answer with the driver that shipped.

Three helpers derive their behaviour from the PostgreSQL driver rather than
restating it -- the replay double's decoded row value, its refusal of an
unbindable parameter, and the query probe's transaction-control arm. Derivation
is the whole point: adding a codec is meant to change all three with nobody
editing them.

They reached for ``wreath._pgdriver`` to do it, which is a *twin* of the
shipped driver on a native build and not the driver itself. The codec table
``register_extension_codec`` writes into belongs to whichever backend loaded, so
the twin's table is empty in the process that registered one -- and the double
then hands back raw bytes for a column the driver decodes.

The one place that is *not* a twin is the class hierarchy: the native
``Connection`` subclasses the pure one, so the Python state machine the probe's
``--ungraft`` arm restores is the native type's own base rather than a second
copy of it. That is pinned here too, because a plan to delete the pure driver
turns on it.
"""

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
    """The defect the double's docstring promises it cannot have.

    `register_extension_codec` is the only way an extension OID becomes
    decodable, and it writes into the *active* backend's table -- so a double
    that decodes through the other module reports `b"[1,2,3]"` for a column a
    real connection returns `[1.0, 2.0, 3.0]` for. That is precisely "a fake
    that scripts the decoded value it wishes it had", inverted: a fake that
    refuses to decode a value the driver does.

    Asserted as the decoded list rather than as equality with the backend's
    `_decode_value`, which the helper now calls and would agree with vacuously.
    """
    pg.register_extension_codec("wreath_probe_vector", PROBE_VECTOR_OID, EXT_KIND_VECTOR)

    assert driver_row_value(PROBE_VECTOR_OID, b"[1,2,3]") == [1.0, 2.0, 3.0]


#: Which module the driver's answers must come from in *this* process. Named by
#: resolution rather than outright, because a build without `_postgres` runs the
#: Python half directly and the contract is the same either way.
_DRIVER = native if pg._implementation == "native" else pure
_OTHER = pure if pg._implementation == "native" else native


def test_the_facade_answers_with_the_driver_that_loaded_not_the_other_one() -> None:
    """The single place that says what the shipped driver does.

    `wreath.postgres` already resolves which backend loaded; these are the same
    resolution applied to the helpers that derive from it. The `is not` half is
    the one that bites on a native build: reaching past the C codec to the base
    class the native driver merely subclasses is invisible until a codec table
    diverges, and then it is a wrong value rather than an error.
    """
    assert pg._decode_value is _DRIVER._decode_value
    assert pg._decode_value is not _OTHER._decode_value
    assert pg._is_transaction_sql is _DRIVER._is_transaction_sql
    assert pg._is_transaction_sql is not _OTHER._is_transaction_sql


def test_the_probe_prices_the_transaction_test_its_own_submit_runs() -> None:
    """`is_txn` is an arm of an A/B, so it has to follow the graft.

    The native `_submit` tests transaction control in C; `--ungraft` puts the
    Python state machine back, and that one calls the module global beside it.
    An arm that priced the Python function in both halves would report a delta
    of zero for a difference that is real, in a tool whose whole claim is that
    its before and after are the same build in the same session.

    On a build without `_postgres` the two arms coincide, and correctly so:
    there is no native pipeline in that process to restore anything from.
    """
    assert query_probe._transaction_test(ungrafted=False) is _DRIVER._is_transaction_sql
    assert query_probe._transaction_test(ungrafted=True) is pure._is_transaction_sql


def test_the_native_connection_inherits_the_python_state_machine() -> None:
    """Characterisation, not a change: the base class is a fact about the build.

    `_native/postgres/connection.c` sets `wreath._pgdriver.Connection` as
    the native type's base and `pipeline.c` resolves its slot offsets, so every
    method `pipeline.c` did not override still runs as Python on a native build.
    The probe's `--ungraft` A/B is exactly "use the base class's versions", and
    reading the base off the type is what keeps that true without naming a
    module that may move.
    """
    assert native.Connection.__mro__[1] is pure.Connection
    assert query_probe._reference_state_machine() is pure

    plan = query_probe._graft_plan(pure.Connection)
    assert [name for name, _original in plan] == list(query_probe.GRAFTED)


def test_a_state_machine_that_has_moved_refuses_the_ab_rather_than_reporting_it() -> (
    None
):
    """The half of `--ungraft` that has to be loud.

    Grafting nothing does not fail: it leaves the native pipeline in place and
    prints a delta of nearly zero against itself, which reads as a measurement.
    `object` stands in for a base that no longer carries the Python state
    machine -- the shape a removal of the pure driver would produce.
    """
    with pytest.raises(SystemExit, match=r"compare the native pipeline against itself"):
        query_probe._graft_plan(object)
