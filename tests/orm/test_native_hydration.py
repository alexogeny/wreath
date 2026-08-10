"""Direct model hydration: field tape straight into model cells, no Record.

These drive the real decoder plan and field tape, so they exercise the same C
path a live query takes. Values, nulls, identity, and failure cleanup must match
the Record path exactly; only the allocations differ.
"""

from __future__ import annotations

import datetime
import gc
import importlib
import struct
import uuid
from typing import Any

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.errors import MappingError
from wreath.orm.model import PERSISTENT, _native
from wreath.orm.registry import Registry
from wreath.orm.types import Bool, Float64, Int64, Text, Timestamp, Uuid

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

requires_native = pytest.mark.skipif(
    _native is None or native is None or not hasattr(native, "_compile_hydrate_plan"),
    reason="native model hydration not built",
)


class Row(Model, table="hydrate_rows"):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text, nullable=True)
    ratio: Mapped[float] = column(Float64, nullable=True)
    flag: Mapped[bool] = column(Bool, nullable=True)
    moment: Mapped[object] = column(Timestamp, nullable=True)
    key: Mapped[object] = column(Uuid, nullable=True)


OIDS = (Int64.oid, Text.oid, Float64.oid, Bool.oid, Timestamp.oid, Uuid.oid)
NAMES = ("id", "label", "ratio", "flag", "moment", "key")

KEY = uuid.UUID("12345678-1234-5678-1234-567812345678")
MOMENT = datetime.datetime(2024, 7, 15, 13, 45, 30)


class FakeDB:
    name = "main"


@pytest.fixture
def registry() -> Registry:
    return Registry(FakeDB(), [Row], validate_schema="off")


@pytest.fixture
def session(registry: Registry) -> Any:
    from wreath.orm.session import Session

    return Session(registry, "read")


def data_row(fields: tuple[bytes | None, ...]) -> bytes:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return bytes(payload)


def encode(
    identifier: int,
    label: str | None = "x",
    ratio: float | None = 1.5,
    flag: bool | None = True,
    moment: datetime.datetime | None = MOMENT,
    key: uuid.UUID | None = KEY,
) -> bytes:
    from wreath import _pgdriver as pure

    return data_row(
        (
            struct.pack("!q", identifier),
            None if label is None else label.encode(),
            None if ratio is None else struct.pack("!d", ratio),
            None if flag is None else (b"\x01" if flag else b"\x00"),
            None if moment is None else pure._encode_binary(moment, Timestamp.oid),
            None if key is None else key.bytes,
        )
    )


def hydrate(
    session: Any,
    registry: Registry,
    payloads: list[bytes],
    *,
    oids: tuple[int, ...] = OIDS,
    targets: tuple[int, ...] = (0, 1, 2, 3, 4, 5),
    formats: tuple[int, ...] | None = None,
) -> tuple[list[Any], Any]:
    """Run payloads through a real tape and decoder plan into models."""
    tape = native._FieldTape(len(oids))
    for payload in payloads:
        tape.append(payload, len(oids))
    decoder = native._compile_decoder_plan(
        oids, formats or (1,) * len(oids), NAMES[: len(oids)]
    )
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), targets)
    rows: list[Any] = []
    native._decode_models(
        decoder, tape, (plan, session._identity, session), 256, rows
    )
    return rows, plan


@requires_native
def test_values_round_trip_through_inline_cells(
    session: Any, registry: Registry
) -> None:
    moment = datetime.datetime(2024, 7, 15, 13, 45, 30, 123456)
    key = KEY
    rows, plan = hydrate(
        session, registry, [encode(7, "hello", -2.25, False, moment, key)]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.id == 7
    assert row.label == "hello"
    assert row.ratio == -2.25
    assert row.flag is False
    assert row.moment == moment
    assert row.key == key
    assert plan.counters == {"allocated": 1, "reused": 0}


@requires_native
def test_hydrated_objects_are_persistent_and_owned(
    session: Any, registry: Registry
) -> None:
    rows, _ = hydrate(session, registry, [encode(1)])
    assert rows[0]._orm_state == PERSISTENT
    assert rows[0]._orm_owner is session
    assert not rows[0]._orm_has_changes()


@requires_native
def test_nulls_set_the_null_bit_rather_than_a_value(
    session: Any, registry: Registry
) -> None:
    rows, _ = hydrate(
        session,
        registry,
        [encode(1, label=None, ratio=None, flag=None, moment=None, key=None)],
    )
    row = rows[0]
    for index in (1, 2, 3, 4, 5):
        assert row._orm_is_loaded(index)
        assert row._orm_is_null(index)
    assert row.label is None and row.ratio is None and row.flag is None
    assert row.moment is None and row.key is None
    assert row.id == 1


@requires_native
def test_no_record_is_allocated_on_the_direct_path(
    session: Any, registry: Registry
) -> None:
    # The Record type must never be instantiated: that is the whole point of
    # the direct path.
    gc.collect()
    before = len([o for o in gc.get_objects() if type(o) is native.Record])
    rows, _ = hydrate(session, registry, [encode(i) for i in range(50)])
    gc.collect()
    after = len([o for o in gc.get_objects() if type(o) is native.Record])
    assert len(rows) == 50
    assert after == before


@requires_native
def test_identity_is_reused_across_batches(session: Any, registry: Registry) -> None:
    first, plan_one = hydrate(session, registry, [encode(3, "a")])
    second, plan_two = hydrate(session, registry, [encode(3, "b")])
    assert first[0] is second[0]
    assert plan_one.counters["allocated"] == 1
    assert plan_two.counters == {"allocated": 0, "reused": 1}
    assert second[0].label == "b"


@requires_native
def test_repeated_rows_for_one_key_hydrate_one_object(
    session: Any, registry: Registry
) -> None:
    rows, plan = hydrate(session, registry, [encode(5), encode(5), encode(5)])
    assert rows[0] is rows[1] is rows[2]
    assert plan.counters == {"allocated": 1, "reused": 2}


@requires_native
def test_a_dirty_field_survives_rehydration(session: Any, registry: Registry) -> None:
    rows, _ = hydrate(session, registry, [encode(9, "original")])
    rows[0].label = "pending"
    assert rows[0]._orm_is_dirty(1)
    again, _ = hydrate(session, registry, [encode(9, "from database")])
    assert again[0] is rows[0]
    assert again[0].label == "pending"


@requires_native
def test_a_null_primary_key_yields_no_object(session: Any, registry: Registry) -> None:
    payload = data_row((None, b"x", struct.pack("!d", 1.0), b"\x01", None, None))
    rows, plan = hydrate(session, registry, [payload])
    assert rows == []
    assert plan.counters["allocated"] == 0


@requires_native
def test_a_projection_hydrates_only_its_columns(
    session: Any, registry: Registry
) -> None:
    tape = native._FieldTape(2)
    tape.append(data_row((struct.pack("!q", 4), b"only")), 2)
    decoder = native._compile_decoder_plan((Int64.oid, Text.oid), (1, 1), ("id", "label"))
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1))
    rows: list[Any] = []
    native._decode_models(decoder, tape, (plan, session._identity, session), 256, rows)
    assert rows[0].id == 4 and rows[0].label == "only"
    assert not rows[0]._orm_is_loaded(2)


@requires_native
def test_text_format_falls_back_to_the_boxed_decoder(
    session: Any, registry: Registry
) -> None:
    # A statement's first execution returns text; the same plan must handle it.
    tape = native._FieldTape(2)
    tape.append(data_row((b"11", b"text-mode")), 2)
    decoder = native._compile_decoder_plan((Int64.oid, Text.oid), (0, 0), ("id", "label"))
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1))
    rows: list[Any] = []
    native._decode_models(decoder, tape, (plan, session._identity, session), 256, rows)
    assert rows[0].id == 11
    assert rows[0].label == "text-mode"


# -- validation and failure ----------------------------------------------------


@requires_native
def test_a_column_count_mismatch_is_rejected_before_the_first_row(
    session: Any, registry: Registry
) -> None:
    tape = native._FieldTape(1)
    tape.append(data_row((struct.pack("!q", 1),)), 1)
    decoder = native._compile_decoder_plan((Int64.oid,), (1,), ("id",))
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1))
    with pytest.raises(MappingError, match="expects"):
        native._decode_models(decoder, tape, (plan, session._identity, session), 256, [])
    assert session._identity == {}


@requires_native
def test_an_oid_mismatch_is_rejected_before_the_first_row(
    session: Any, registry: Registry
) -> None:
    tape = native._FieldTape(2)
    tape.append(data_row((struct.pack("!q", 1), b"x")), 2)
    decoder = native._compile_decoder_plan((Int64.oid, Int64.oid), (1, 1), ("id", "label"))
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1))
    with pytest.raises(MappingError, match="OID"):
        native._decode_models(decoder, tape, (plan, session._identity, session), 256, [])
    assert session._identity == {}


@requires_native
def test_a_model_result_without_its_primary_key_is_rejected(registry: Registry) -> None:
    with pytest.raises(MappingError, match="primary key"):
        native._compile_hydrate_plan(Row, registry.spec_for(Row), (1, 2))


@requires_native
def test_malformed_data_leaves_no_partially_visible_object(
    session: Any, registry: Registry
) -> None:
    # A believable id, then a bool field of the wrong width.
    payload = data_row(
        (
            struct.pack("!q", 42),
            b"x",
            struct.pack("!d", 1.0),
            b"\x01\x02\x03",
            None,
            None,
        )
    )
    rows: list[Any] = []
    tape = native._FieldTape(len(OIDS))
    tape.append(payload, len(OIDS))
    decoder = native._compile_decoder_plan(OIDS, (1,) * len(OIDS), NAMES)
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1, 2, 3, 4, 5))
    with pytest.raises(ValueError):
        native._decode_models(
            decoder, tape, (plan, session._identity, session), 256, rows
        )
    # Nothing half-built escapes: no object in the list, none in the identity map.
    assert rows == []
    assert session._identity == {}
    gc.collect()


@requires_native
def test_a_failure_partway_through_a_batch_publishes_no_rows(
    session: Any, registry: Registry
) -> None:
    good = encode(1)
    bad = data_row(
        (struct.pack("!q", 2), b"x", struct.pack("!d", 1.0), b"\x01\x02", None, None)
    )
    rows: list[Any] = []
    tape = native._FieldTape(len(OIDS))
    tape.append(good, len(OIDS))
    tape.append(bad, len(OIDS))
    decoder = native._compile_decoder_plan(OIDS, (1,) * len(OIDS), NAMES)
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1, 2, 3, 4, 5))
    with pytest.raises(ValueError):
        native._decode_models(
            decoder, tape, (plan, session._identity, session), 256, rows
        )
    # The batch is all-or-nothing for the caller's list.
    assert rows == []
    gc.collect()


@requires_native
def test_infinity_timestamps_are_rejected_not_wrapped(
    session: Any, registry: Registry
) -> None:
    payload = data_row(
        (
            struct.pack("!q", 1),
            b"x",
            struct.pack("!d", 1.0),
            b"\x01",
            struct.pack("!q", 2**63 - 1),
            None,
        )
    )
    tape = native._FieldTape(len(OIDS))
    tape.append(payload, len(OIDS))
    decoder = native._compile_decoder_plan(OIDS, (1,) * len(OIDS), NAMES)
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1, 2, 3, 4, 5))
    with pytest.raises(ValueError, match="infinity"):
        native._decode_models(decoder, tape, (plan, session._identity, session), 256, [])


@requires_native
def test_hydrated_objects_survive_collection(session: Any, registry: Registry) -> None:
    rows, _ = hydrate(session, registry, [encode(i, "x" * i) for i in range(200)])
    gc.collect()
    assert len(rows) == 200
    assert rows[100].id == 100
    del rows
    session._identity.clear()
    gc.collect()


@requires_native
@pytest.mark.asyncio
async def test_every_batch_reaches_the_model_destination_not_only_the_last(
    session: Any, registry: Registry
) -> None:
    """Results larger than one batch must be models throughout.

    The native parser decodes a batch as soon as the tape reaches 256 rows,
    without returning to Python. That in-C flush has to honour the operation's
    destination: when it did not, each full batch decoded to Records and only
    the trailing partial batch was hydrated, so any result over 256 rows came
    back as a mixture. Unit-testing _decode_models directly cannot see this --
    it takes the parser out of the picture -- so this drives the parser.
    """
    protocol = native.BufferedProtocol()
    plan = native._compile_hydrate_plan(Row, registry.spec_for(Row), (0, 1))
    tape = native._FieldTape(2)
    decoder = native._compile_decoder_plan((Int64.oid, Text.oid), (1, 1), ("id", "label"))
    rows: list[Any] = []

    class _Operation:
        mode = "fetch"
        discarded = False
        command = ""

        def __init__(self) -> None:
            self.field_tape = tape
            self.decoder_plan = decoder
            self.rows = rows
            self.dest = (plan, session._identity, session)

    protocol.register_operations((_Operation(),))
    total = 600
    for value in range(total):
        payload = data_row((struct.pack("!q", value), f"label-{value}".encode()))
        message = b"D" + struct.pack("!I", len(payload) + 4) + payload
        view = protocol.get_buffer(-1)
        view[: len(message)] = message
        del view
        protocol.buffer_updated(len(message))

    # Two full batches were flushed inside the parser; the rest waits on the
    # tape for the driver to flush at ReadyForQuery.
    assert len(rows) == 512
    assert tape.row_count == total - 512
    assert {type(item).__name__ for item in rows} == {"Row"}
    assert [item.id for item in rows[:3]] == [0, 1, 2]
    assert rows[511].label == "label-511"
    assert len(session._identity) == 512
