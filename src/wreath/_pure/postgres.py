"""Dependency-free asynchronous PostgreSQL reference driver."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import hmac
import os
import secrets
import struct
import sys
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, NamedTuple, overload
from urllib.parse import parse_qs, unquote, urlsplit

_IMPLEMENTATION = "pure"
_implementation = _IMPLEMENTATION
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024

# LISTEN/NOTIFY. Async NotificationResponse ('A') frames are captured into a
# bounded per-connection ring rather than discarded, so a consumer can drain
# them via ``Connection.notifications()``. The ring is intentionally lossy:
# correctness never depends on a notification arriving (the durable-queue path
# treats NOTIFY as a latency doorbell and still polls the table), so on overflow
# the oldest notification is dropped and a counter is bumped instead of letting
# an idle-but-noisy channel grow memory without bound.
_NOTIFY_RING_CAPACITY = 1024
# NAMEDATALEN - 1; PostgreSQL truncates longer identifiers, so reject them up
# front rather than silently listen on a different channel than requested.
_MAX_CHANNEL_NAME = 63


class Notification(NamedTuple):
    """A single asynchronous ``NOTIFY`` delivered to a listening connection."""

    pid: int
    channel: str
    payload: str


def _validate_channel(channel: str) -> str:
    """Validate a LISTEN/UNLISTEN channel name.

    The name is emitted as a double-quoted identifier, so restricting it to
    identifier characters (and bounding its length) keeps the emitted SQL
    injection-free and preserves the exact case the caller asked for.
    """
    if not isinstance(channel, str) or not channel:
        raise ValueError("channel name must be a non-empty string")
    if len(channel.encode("utf-8")) > _MAX_CHANNEL_NAME:
        raise ValueError(f"channel name exceeds {_MAX_CHANNEL_NAME} bytes")
    for character in channel:
        if not (character.isalnum() or character in "_$"):
            raise ValueError(f"invalid channel name character: {character!r}")
    return channel


def _parse_notification(payload: bytes) -> Notification | None:
    """Decode a NotificationResponse body: int32 pid, then two C strings.

    Returns ``None`` for a malformed frame (too short, or missing the channel
    terminator) so a corrupt async message is dropped rather than raised into
    the reader loop, where it would tear down an otherwise healthy connection.
    """
    if len(payload) < 4:
        return None
    pid = int.from_bytes(payload[:4], "big")
    channel, separator, tail = payload[4:].partition(b"\x00")
    if not separator:
        return None
    body = tail.partition(b"\x00")[0]
    return Notification(
        pid,
        channel.decode("utf-8", "replace"),
        body.decode("utf-8", "replace"),
    )

_BOOL = 16
_BYTEA = 17
_INT8 = 20
_INT2 = 21
_INT4 = 23
_TEXT = 25
_JSON = 114
_FLOAT4 = 700
_FLOAT8 = 701
_VARCHAR = 1043
_DATE = 1082
_TIMESTAMP = 1114
_TIMESTAMPTZ = 1184
_NUMERIC = 1700
_UUID = 2950
_JSONB = 3802

# `numeric` is base-10000 digit groups plus a sign and a display scale, which is
# what lets it hold values no float can. `float()` was the wrong landing type,
# not merely a lossy one: it collapses distinct values onto one.
_NUMERIC_POS = 0x0000
_NUMERIC_NEG = 0x4000
_NUMERIC_NAN = 0xC000
_NUMERIC_PINF = 0xD000
_NUMERIC_NINF = 0xF000

# PostgreSQL stores date/timestamp binary values relative to 2000-01-01, and
# reserves the int64/int32 extremes for the infinity values datetime cannot
# represent.
_PG_EPOCH_DATE = datetime.date(2000, 1, 1)
_PG_EPOCH_NAIVE = datetime.datetime(2000, 1, 1)
_PG_EPOCH_UTC = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
_TIMESTAMP_INFINITY = 0x7FFFFFFFFFFFFFFF
_TIMESTAMP_NEGATIVE_INFINITY = -0x8000000000000000
_DATE_INFINITY = 0x7FFFFFFF
_DATE_NEGATIVE_INFINITY = -0x80000000


class PostgresError(Exception):
    """An error reported by PostgreSQL.

    A subclass that stands for exactly one SQLSTATE declares it as a class
    attribute; anything raised from a server ``ErrorResponse`` passes the code
    it actually carried. Both spellings answer to ``.sqlstate``.
    """

    #: The condition this class always means, or ``None`` when it depends on
    #: what the server said. Declared here so `.sqlstate` resolves on every
    #: instance without the constructor having to write one.
    sqlstate: str | None = None

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        # Only shadow the class attribute when there is something to shadow it
        # with. Assigning unconditionally set `self.sqlstate = None` over a
        # subclass's own declaration, so the classes that named their condition
        # were exactly the ones that lost it, and every caller classifying by
        # sqlstate read `None` for them. The workaround -- pass it back through
        # `super().__init__` -- was load-bearing and undiscoverable; it is now
        # merely one of two spellings that work.
        if sqlstate is not None:
            self.sqlstate = sqlstate


class InterfaceError(PostgresError):
    """Invalid driver use or connection state."""


class OperationalError(PostgresError):
    """Transport, authentication, or protocol operation failure."""


class ProtocolError(OperationalError):
    """Malformed or unexpected PostgreSQL wire data."""


@dataclass(frozen=True, slots=True)
class Plan:
    statement_name: bytes
    parameter_oids: tuple[int, ...]
    result_oids: tuple[int, ...]
    result_names: tuple[str, ...]


def _plan_retained_bytes(sql: str, plan: Plan) -> int:
    size = sys.getsizeof(sql) + sys.getsizeof(plan)
    for value in (
        plan.statement_name,
        plan.parameter_oids,
        plan.result_oids,
        plan.result_names,
    ):
        size += sys.getsizeof(value)
    size += sum(sys.getsizeof(name) for name in plan.result_names)
    return size


class Record(Sequence[object]):
    """Immutable result row with positional and column-name access."""

    __slots__ = ("_index", "_values")

    def __init__(self, names: tuple[str, ...], values: tuple[object, ...]) -> None:
        self._values = values
        self._index = {name: index for index, name in enumerate(names)}

    @overload
    def __getitem__(self, key: int) -> object: ...

    @overload
    def __getitem__(self, key: slice) -> Sequence[object]: ...

    @overload
    def __getitem__(self, key: str) -> object: ...

    def __getitem__(self, key: int | slice | str) -> object | Sequence[object]:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def __repr__(self) -> str:
        return f"Record({self._values!r})"


@dataclass(frozen=True, slots=True)
class _ConnectInfo:
    user: str
    password: str | None
    database: str
    host: str
    port: int | str
    unix: bool


def _parse_dsn(dsn: str) -> _ConnectInfo:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise InterfaceError("DSN scheme must be postgres or postgresql")
    query = parse_qs(parsed.query, keep_blank_values=True)
    user = unquote(parsed.username or os.environ.get("USER", "postgres"))
    password = unquote(parsed.password) if parsed.password is not None else None
    database = unquote(parsed.path.lstrip("/")) or user
    query_host = query.get("host", [None])[-1]
    host = unquote(query_host) if query_host is not None else (parsed.hostname or "127.0.0.1")
    query_port = query.get("port", [None])[-1]
    if host.startswith("/"):
        socket_name = unquote(query_port) if query_port else ".s.PGSQL.5432"
        if socket_name.isdecimal():
            socket_name = f".s.PGSQL.{socket_name}"
        return _ConnectInfo(user, password, database, f"{host.rstrip('/')}/{socket_name}", 0, True)
    try:
        port = int(query_port) if query_port is not None else (parsed.port or 5432)
    except ValueError as error:
        raise InterfaceError("invalid PostgreSQL port") from error
    if not 0 < port < 65536:
        raise InterfaceError("PostgreSQL port must be in 1..65535")
    return _ConnectInfo(user, password, database, host, port, False)


def _cstring(value: str | bytes) -> bytes:
    encoded = value.encode() if isinstance(value, str) else value
    if b"\x00" in encoded:
        raise ValueError("PostgreSQL strings cannot contain NUL")
    return encoded + b"\x00"


def _message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


async def _read_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind = await reader.readexactly(1)
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if length < 4 or length > _MAX_MESSAGE_BYTES:
        raise ProtocolError(f"invalid backend message length {length}")
    return kind, await reader.readexactly(length - 4)


def _infer_oid(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return _BOOL
    if isinstance(value, int):
        return _INT4 if -(2**31) <= value < 2**31 else _INT8
    if isinstance(value, float):
        return _FLOAT8
    if isinstance(value, str):
        return _TEXT
    if isinstance(value, bytes):
        return _BYTEA
    if isinstance(value, uuid.UUID):
        return _UUID
    if isinstance(value, Decimal):
        return _NUMERIC
    # datetime is a date subclass, so it must be tested first.
    if isinstance(value, datetime.datetime):
        return _TIMESTAMP if value.tzinfo is None else _TIMESTAMPTZ
    if isinstance(value, datetime.date):
        return _DATE
    if isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(
            f"unsupported PostgreSQL value type: {type(value).__name__}. "
            "Inference binds one scalar per placeholder and a sequence has no "
            "inferable element type -- [1, 2] is equally int4[], int8[] or "
            "numeric[], and [] names no element type at all. Write the predicate "
            "as IN ($1, $2, ...) with one placeholder per value rather than "
            "= ANY($1); that is what Wreath's own readers do, and it is bounded "
            "by however many values you actually have. An array *column* is a "
            "different path: its OID comes from the declaration, not from here."
        )
    raise TypeError(f"unsupported PostgreSQL value type: {type(value).__name__}")


def _as_date(value: object) -> datetime.date:
    if type(value) is not datetime.date:
        raise TypeError("date codec requires date")
    return value


def _as_timestamp(value: object, *, aware: bool) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise TypeError("timestamp codec requires datetime")
    if aware and value.tzinfo is None:
        raise TypeError("timestamptz codec requires an aware datetime")
    if not aware and value.tzinfo is not None:
        raise TypeError("timestamp codec requires a naive datetime")
    return value


def _as_json(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("json codec requires str")
    return value


def _as_decimal(value: object) -> Decimal:
    """Accept the exact numeric types only.

    ``float`` is refused rather than converted: a column is declared ``numeric``
    precisely because binary floating point cannot hold its values, so accepting
    a float here would reintroduce the collapse the type exists to prevent.
    """
    if isinstance(value, Decimal):
        return value
    if type(value) is int:
        return Decimal(value)
    raise TypeError(
        "numeric codec requires Decimal or int, not "
        f"{type(value).__name__}; a float cannot hold a numeric exactly"
    )


def _encode_numeric(value: object) -> bytes:
    """Pack a ``Decimal`` into PostgreSQL's base-10000 numeric wire form.

    Layout: ``ndigits``, ``weight``, ``sign``, ``dscale``, then ``ndigits``
    base-10000 groups, most significant first. The value is
    ``sum(digit[i] * 10000 ** (weight - i))`` and ``dscale`` is how many decimal
    places to display -- carried separately, which is why ``Decimal("1.10")``
    survives as ``1.10`` rather than ``1.1``.
    """
    number = _as_decimal(value)
    sign, digit_tuple, exponent = number.as_tuple()
    if not isinstance(exponent, int):  # 'n'/'N' (NaN) or 'F' (infinity)
        if exponent == "F":
            marker = _NUMERIC_NINF if sign else _NUMERIC_PINF
        else:
            marker = _NUMERIC_NAN
        return struct.pack("!hhHH", 0, 0, marker, 0)

    unscaled = int("".join(map(str, digit_tuple))) if digit_tuple else 0
    if exponent > 0:
        # A positive exponent is a whole-number magnitude; multiply it out so
        # the group split below always works against a plain integer.
        unscaled *= 10**exponent
        exponent = 0
    dscale = -exponent

    # Pad the fractional part out to a whole number of base-10000 groups so the
    # split lands on a group boundary.
    padding = (-dscale) % 4
    unscaled *= 10**padding
    fraction_groups = (dscale + padding) // 4

    groups: list[int] = []
    while unscaled:
        unscaled, remainder = divmod(unscaled, 10000)
        groups.append(remainder)
    groups.reverse()

    weight = len(groups) - 1 - fraction_groups
    # PostgreSQL sends no leading or trailing zero groups; dscale still records
    # the display scale, so stripping them cannot lose the value's precision.
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    if not groups:
        # PostgreSQL has no signed zero -- `SELECT '-0'::numeric` is `0` -- so a
        # negative zero is sent positive rather than as a sign the server would
        # never itself produce.
        weight = 0
        sign = 0

    header = struct.pack(
        "!hhHH", len(groups), weight, _NUMERIC_NEG if sign else _NUMERIC_POS, dscale
    )
    return header + struct.pack(f"!{len(groups)}h", *groups)


def _decode_numeric(data: bytes) -> Decimal:
    """Unpack PostgreSQL's binary numeric into an exact ``Decimal``."""
    if len(data) < 8:
        raise ProtocolError("invalid binary numeric header")
    ndigits, weight, sign, dscale = struct.unpack_from("!hhHH", data, 0)
    if sign == _NUMERIC_NAN:
        return Decimal("NaN")
    if sign == _NUMERIC_PINF:
        return Decimal("Infinity")
    if sign == _NUMERIC_NINF:
        return Decimal("-Infinity")
    if sign not in {_NUMERIC_POS, _NUMERIC_NEG}:
        raise ProtocolError(f"invalid numeric sign 0x{sign:04X}")
    if ndigits < 0 or len(data) != 8 + ndigits * 2:
        raise ProtocolError("invalid binary numeric length")

    unscaled = 0
    for group in struct.unpack_from(f"!{ndigits}h", data, 8) if ndigits else ():
        if not 0 <= group < 10000:
            raise ProtocolError("invalid numeric digit group")
        unscaled = unscaled * 10000 + group
    exponent = 4 * (weight - ndigits + 1)

    # Move the exponent onto the advertised display scale. Growing is always
    # safe; shrinking only ever drops PostgreSQL's zero group padding, and the
    # `% 10` guard means a significant digit is never discarded.
    while exponent > -dscale:
        unscaled *= 10
        exponent -= 1
    while exponent < -dscale and unscaled % 10 == 0:
        unscaled //= 10
        exponent += 1
    return Decimal(f"{'-' if sign == _NUMERIC_NEG else ''}{unscaled}E{exponent}")


def _encode_text(value: object, oid: int) -> bytes | None:
    if value is None:
        return None
    if oid == _BOOL:
        if type(value) is not bool:
            raise TypeError("bool codec requires bool")
        return b"t" if value else b"f"
    if oid in {_INT2, _INT4, _INT8}:
        if type(value) is not int:
            raise TypeError("integer codec requires int")
        return str(value).encode("ascii")
    if oid in {_FLOAT4, _FLOAT8}:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("float codec requires int or float")
        return repr(float(value)).encode("ascii")
    if oid in {_TEXT, _VARCHAR}:
        if not isinstance(value, str):
            raise TypeError("text codec requires str")
        return value.encode("utf-8")
    if oid == _BYTEA:
        if not isinstance(value, bytes):
            raise TypeError("bytea codec requires bytes")
        return b"\\x" + value.hex().encode("ascii")
    if oid == _UUID:
        if not isinstance(value, uuid.UUID):
            raise TypeError("uuid codec requires UUID")
        return str(value).encode("ascii")
    if oid == _DATE:
        return _as_date(value).isoformat().encode("ascii")
    if oid == _TIMESTAMP:
        return _as_timestamp(value, aware=False).isoformat(sep=" ").encode("ascii")
    if oid == _TIMESTAMPTZ:
        return _as_timestamp(value, aware=True).isoformat(sep=" ").encode("ascii")
    if oid == _NUMERIC:
        return str(_as_decimal(value)).encode("ascii")
    if oid in {_JSON, _JSONB}:
        return _as_json(value).encode("utf-8")
    return str(value).encode("utf-8")


def _encode_binary(value: object, oid: int) -> bytes | None:
    if value is None:
        return None
    if oid == _BOOL:
        if type(value) is not bool:
            raise TypeError("bool codec requires bool")
        return b"\x01" if value else b"\x00"
    if oid == _INT2:
        if type(value) is not int:
            raise TypeError("int2 codec requires int")
        if not -(2**15) <= value < 2**15:
            raise OverflowError("int2 out of range")
        return struct.pack("!h", value)
    if oid == _INT4:
        if type(value) is not int:
            raise TypeError("int4 codec requires int")
        if not -(2**31) <= value < 2**31:
            raise OverflowError("int4 out of range")
        return struct.pack("!i", value)
    if oid == _INT8:
        if type(value) is not int:
            raise TypeError("int8 codec requires int")
        if not -(2**63) <= value < 2**63:
            raise OverflowError("int8 out of range")
        return struct.pack("!q", value)
    if oid == _FLOAT4:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("float4 codec requires int or float")
        return struct.pack("!f", value)
    if oid == _FLOAT8:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("float8 codec requires int or float")
        return struct.pack("!d", value)
    if oid in {_TEXT, _VARCHAR}:
        if not isinstance(value, str):
            raise TypeError("text codec requires str")
        return value.encode("utf-8")
    if oid == _BYTEA:
        if not isinstance(value, bytes):
            raise TypeError("bytea codec requires bytes")
        return value
    if oid == _UUID:
        if not isinstance(value, uuid.UUID):
            raise TypeError("uuid codec requires UUID")
        return value.bytes
    if oid == _DATE:
        return struct.pack("!i", (_as_date(value) - _PG_EPOCH_DATE).days)
    if oid in {_TIMESTAMP, _TIMESTAMPTZ}:
        aware = oid == _TIMESTAMPTZ
        moment = _as_timestamp(value, aware=aware)
        delta = moment - (_PG_EPOCH_UTC if aware else _PG_EPOCH_NAIVE)
        return struct.pack(
            "!q", delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        )
    if oid == _NUMERIC:
        return _encode_numeric(value)
    if oid == _JSON:
        return _as_json(value).encode("utf-8")
    if oid == _JSONB:
        return b"\x01" + _as_json(value).encode("utf-8")
    raise TypeError(f"no binary encoder for PostgreSQL OID {oid}")


def _decode_value(oid: int, format_code: int, data: bytes | None) -> object:
    if data is None:
        return None
    if format_code == 0:
        if oid == _BOOL:
            return data == b"t"
        if oid in {_INT2, _INT4, _INT8}:
            return int(data)
        if oid in {_FLOAT4, _FLOAT8}:
            return float(data)
        if oid in {_TEXT, _VARCHAR}:
            return data.decode("utf-8")
        if oid == _BYTEA:
            return bytes.fromhex(data[2:].decode("ascii")) if data.startswith(b"\\x") else data
        if oid == _UUID:
            return uuid.UUID(data.decode("ascii"))
        if oid == _DATE:
            return datetime.date.fromisoformat(data.decode("ascii"))
        if oid in {_TIMESTAMP, _TIMESTAMPTZ}:
            return datetime.datetime.fromisoformat(data.decode("ascii"))
        if oid == _NUMERIC:
            return Decimal(data.decode("ascii"))
        if oid in {_JSON, _JSONB}:
            return data.decode("utf-8")
        return data
    if format_code != 1:
        raise ProtocolError(f"invalid field format code {format_code}")
    if oid == _BOOL:
        if len(data) != 1:
            raise ProtocolError("invalid binary bool")
        return data != b"\x00"
    sizes = {
        _INT2: ("!h", 2),
        _INT4: ("!i", 4),
        _INT8: ("!q", 8),
        _FLOAT4: ("!f", 4),
        _FLOAT8: ("!d", 8),
    }
    if oid in sizes:
        fmt, size = sizes[oid]
        if len(data) != size:
            raise ProtocolError(f"invalid binary value length for OID {oid}")
        return struct.unpack(fmt, data)[0]
    if oid in {_TEXT, _VARCHAR}:
        return data.decode("utf-8")
    if oid == _BYTEA:
        return data
    if oid == _UUID:
        if len(data) != 16:
            raise ProtocolError("invalid binary uuid")
        return uuid.UUID(bytes=data)
    if oid == _DATE:
        if len(data) != 4:
            raise ProtocolError("invalid binary date length")
        days = struct.unpack("!i", data)[0]
        if days in {_DATE_INFINITY, _DATE_NEGATIVE_INFINITY}:
            raise ProtocolError("date infinity is not representable")
        return _PG_EPOCH_DATE + datetime.timedelta(days=days)
    if oid in {_TIMESTAMP, _TIMESTAMPTZ}:
        if len(data) != 8:
            raise ProtocolError(f"invalid binary value length for OID {oid}")
        micros = struct.unpack("!q", data)[0]
        if micros in {_TIMESTAMP_INFINITY, _TIMESTAMP_NEGATIVE_INFINITY}:
            raise ProtocolError("timestamp infinity is not representable")
        epoch = _PG_EPOCH_UTC if oid == _TIMESTAMPTZ else _PG_EPOCH_NAIVE
        return epoch + datetime.timedelta(microseconds=micros)
    if oid == _NUMERIC:
        return _decode_numeric(data)
    if oid == _JSON:
        return data.decode("utf-8")
    if oid == _JSONB:
        if len(data) < 1 or data[0] != 1:
            raise ProtocolError("unsupported jsonb wire version")
        return data[1:].decode("utf-8")
    return data


def _bind_payload(
    statement_name: bytes,
    args: tuple[object, ...],
    oids: tuple[int, ...],
    *,
    binary_parameters: bool,
    binary_results: bool,
) -> bytes:
    if len(args) != len(oids):
        raise InterfaceError("query argument count does not match plan")
    payload = bytearray(b"\x00" + _cstring(statement_name))
    if binary_parameters and args:
        payload += struct.pack("!H", len(args)) + b"\x00\x01" * len(args)
    else:
        payload += struct.pack("!H", 0)
    payload += struct.pack("!H", len(args))
    for value, oid in zip(args, oids, strict=True):
        encoded = _encode_binary(value, oid) if binary_parameters else _encode_text(value, oid)
        if encoded is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(encoded)) + encoded
    if binary_results:
        payload += struct.pack("!HH", 1, 1)
    else:
        payload += struct.pack("!H", 0)
    return bytes(payload)


def _build_cold_query_packet(
    statement_name: bytes,
    sql: str,
    args: tuple[object, ...],
    parameter_oids: tuple[int, ...],
    mode: str,
    *,
    binary_results: bool = False,
) -> bytes:
    """Parse, describe, bind and execute a statement seen for the first time.

    Results come back as *text* by default, because a cold operation has not yet
    learned its result OIDs and the text decoder handles anything. A caller whose
    destination can only read binary -- the catalog image decoder is the one --
    passes ``binary_results=True``, and the Describe(Portal) reply then reports
    format 1 so the compiled decoder plan matches what the server will send.

    Getting this wrong does not produce a wrong answer, it produces a *hang*: the
    decoder raises inside the reader task, which is not the caller's task, so the
    caller waits on a future nobody will ever resolve. That is why the format is
    a parameter rather than something the destination checks after the fact.
    """
    if mode not in {"execute", "fetch", "fetchrow", "fetchval"}:
        raise ValueError(f"unknown PostgreSQL result mode {mode!r}")
    parse = _cstring(statement_name) + _cstring(sql) + struct.pack("!H", len(args))
    parse += b"".join(struct.pack("!I", 0) for _ in args)
    messages = [
        _message(b"P", parse),
        _message(b"D", b"S" + _cstring(statement_name)),
        _message(
            b"B",
            _bind_payload(
                statement_name,
                args,
                parameter_oids,
                binary_parameters=False,
                binary_results=binary_results and mode != "execute",
            ),
        ),
    ]
    if mode != "execute":
        messages.append(_message(b"D", b"P\x00"))
    messages.extend((_message(b"E", b"\x00\x00\x00\x00\x00"), _message(b"S")))
    return b"".join(messages)


def _build_cached_query_packet(plan: Plan, args: tuple[object, ...], mode: str) -> bytes:
    if mode not in {"execute", "fetch", "fetchrow", "fetchval"}:
        raise ValueError(f"unknown PostgreSQL result mode {mode!r}")
    return b"".join(
        (
            _message(
                b"B",
                _bind_payload(
                    plan.statement_name,
                    args,
                    plan.parameter_oids,
                    binary_parameters=True,
                    binary_results=mode != "execute",
                ),
            ),
            _message(b"E", b"\x00\x00\x00\x00\x00"),
            _message(b"S"),
        )
    )


@dataclass(slots=True)
class _ScramState:
    client_nonce: str
    client_first_bare: str


def _scram_start(user: str, nonce: str | None = None) -> tuple[_ScramState, str]:
    nonce = nonce or secrets.token_urlsafe(24).rstrip("=")
    escaped_user = user.replace("=", "=3D").replace(",", "=2C")
    bare = f"n={escaped_user},r={nonce}"
    return _ScramState(nonce, bare), f"n,,{bare}"


def _scram_continue(state: _ScramState, password: str, server_first: str) -> tuple[str, str]:
    attributes = dict(item.split("=", 1) for item in server_first.split(",") if "=" in item)
    nonce = attributes.get("r", "")
    if not nonce.startswith(state.client_nonce) or nonce == state.client_nonce:
        raise OperationalError("invalid SCRAM server nonce")
    try:
        salt = base64.b64decode(attributes["s"], validate=True)
        iterations = int(attributes["i"])
    except (KeyError, ValueError) as error:
        raise OperationalError("invalid SCRAM challenge") from error
    if iterations <= 0:
        raise OperationalError("invalid SCRAM iteration count")
    client_final_bare = f"c=biws,r={nonce}"
    auth_message = f"{state.client_first_bare},{server_first},{client_final_bare}".encode()
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    client_signature = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
    proof = bytes(left ^ right for left, right in zip(client_key, client_signature, strict=True))
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    server_signature = hmac.new(server_key, auth_message, hashlib.sha256).digest()
    final = f"{client_final_bare},p={base64.b64encode(proof).decode()}"
    return final, base64.b64encode(server_signature).decode()


def _scram_finish(expected_signature: str, server_final: str) -> None:
    attributes = dict(item.split("=", 1) for item in server_final.split(",") if "=" in item)
    if "e" in attributes:
        raise OperationalError(f"SCRAM authentication failed: {attributes['e']}")
    if not hmac.compare_digest(attributes.get("v", ""), expected_signature):
        raise OperationalError("invalid SCRAM server signature")


def _parse_error(payload: bytes) -> PostgresError:
    fields: dict[str, str] = {}
    for field in payload.rstrip(b"\x00").split(b"\x00"):
        if field:
            fields[chr(field[0])] = field[1:].decode("utf-8", "replace")
    return PostgresError(fields.get("M", "PostgreSQL error"), sqlstate=fields.get("C"))


def _parse_parameter_description(payload: bytes) -> tuple[int, ...]:
    if len(payload) < 2:
        raise ProtocolError("truncated ParameterDescription")
    count = struct.unpack_from("!H", payload)[0]
    if len(payload) != 2 + count * 4:
        raise ProtocolError("invalid ParameterDescription length")
    return tuple(struct.unpack_from("!I", payload, 2 + index * 4)[0] for index in range(count))


def _parse_row_description(
    payload: bytes,
) -> tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...]]:
    if len(payload) < 2:
        raise ProtocolError("truncated RowDescription")
    count = struct.unpack_from("!H", payload)[0]
    offset = 2
    names: list[str] = []
    oids: list[int] = []
    formats: list[int] = []
    for _ in range(count):
        end = payload.find(b"\x00", offset)
        if end < 0 or end + 19 > len(payload):
            raise ProtocolError("truncated RowDescription field")
        names.append(payload[offset:end].decode("utf-8"))
        offset = end + 1
        _, _, oid, _, _, format_code = struct.unpack_from("!IhIhih", payload, offset)
        offset += 18
        oids.append(oid)
        formats.append(format_code)
    if offset != len(payload):
        raise ProtocolError("invalid RowDescription length")
    return tuple(names), tuple(oids), tuple(formats)


def _data_fields(payload: bytes) -> list[bytes | None]:
    if len(payload) < 2:
        raise ProtocolError("truncated DataRow")
    count = struct.unpack_from("!H", payload)[0]
    offset = 2
    fields: list[bytes | None] = []
    for _ in range(count):
        if offset + 4 > len(payload):
            raise ProtocolError("truncated DataRow field length")
        length = struct.unpack_from("!i", payload, offset)[0]
        offset += 4
        if length == -1:
            fields.append(None)
        elif length < 0 or offset + length > len(payload):
            raise ProtocolError("invalid DataRow field length")
        else:
            fields.append(payload[offset : offset + length])
            offset += length
    if offset != len(payload):
        raise ProtocolError("invalid DataRow length")
    return fields


def _join_pipeline_packets(packets: tuple[bytes, ...]) -> bytes:
    return b"".join(packets)


class PipelineFullError(InterfaceError):
    """The connection's bounded pipeline cannot accept another operation."""


ResultMode = Literal["execute", "fetch", "fetchrow", "fetchval"]


class Operation:
    """One Sync-delimited operation in the connection pipeline."""

    __slots__ = (
        "args",
        "cold",
        "command",
        "deadline",
        "decoder_plan",
        "dest",
        "discarded",
        "error",
        "field_tape",
        "future",
        "have_value",
        "mode",
        "one_row",
        "one_value",
        "packet",
        "parameter_oids",
        "plan",
        "result_formats",
        "result_names",
        "result_oids",
        "rows",
        "sequence",
        "sql",
        "state",
        "statement_name",
    )

    def __init__(
        self,
        sequence: int,
        sql: str,
        args: tuple[object, ...],
        mode: ResultMode,
        future: asyncio.Future[Any],
        deadline: float | None,
    ) -> None:
        self.sequence = sequence
        self.sql = sql
        self.args = args
        self.mode = mode
        self.future = future
        self.deadline = deadline
        self.decoder_plan: Any = None
        self.dest: Any = None
        self.field_tape: Any = None
        self.state = "waiting"
        self.plan: Plan | None = None
        self.cold = True
        self.statement_name = b""
        self.packet = b""
        self.parameter_oids: tuple[int, ...] = ()
        self.result_names: tuple[str, ...] = ()
        self.result_oids: tuple[int, ...] = ()
        self.result_formats: tuple[int, ...] = ()
        self.rows: list[Record] | None = [] if mode == "fetch" else None
        self.one_row: Record | None = None
        self.one_value: object = None
        self.have_value = False
        self.command = ""
        self.error: PostgresError | None = None
        self.discarded = False


class _Transaction:
    """An explicit ``BEGIN``/``COMMIT``/``ROLLBACK`` scope over a connection.

    Operations issued through the transaction run in order (the connection
    rejects concurrent operations once a transaction is open), which is what
    makes read-before-write ordering explicit for read-modify-write workloads.
    """

    __slots__ = ("_connection", "_open")

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._open = False

    async def __aenter__(self) -> _Transaction:
        await self._connection.execute("BEGIN")
        self._open = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if not self._open:
            return False
        self._open = False
        if exc_type is None:
            await self._connection.execute("COMMIT")
            return False
        # Roll back on any failure. If the rollback itself cannot complete, the
        # connection's failure path has already marked it unusable, so the pool
        # discards it rather than reusing an unsynchronized connection.
        with contextlib.suppress(Exception):
            await self._connection.execute("ROLLBACK")
        return False

    async def execute(self, statement: object, *args: object) -> str:
        return await self._connection.execute(_sql_of(statement), *args)

    async def fetch(self, statement: object, *args: object) -> list[Record]:
        return await self._connection.fetch(_sql_of(statement), *args)

    async def fetchrow(self, statement: object, *args: object) -> Record | None:
        return await self._connection.fetchrow(_sql_of(statement), *args)

    async def fetchval(self, statement: object, *args: object) -> object:
        return await self._connection.fetchval(_sql_of(statement), *args)

    async def map(
        self,
        method: ResultMode,
        statement: object,
        argument_sets: Any,
        *,
        max_in_flight: int = 32,
    ) -> list[Any]:
        return await self._connection.map(
            method, statement, argument_sets, max_in_flight=max_in_flight
        )


def _sql_of(statement: object) -> str:
    sql = getattr(statement, "sql", statement)
    if not isinstance(sql, str):
        raise InterfaceError("statement must be SQL text or expose .sql")
    return sql


class Connection:
    """Bounded automatically-pipelined asynchronous PostgreSQL connection."""

    _record_type = Record
    _plan_type = Plan
    _operation_type = Operation
    _decode = staticmethod(_decode_value)
    _build_cold = staticmethod(_build_cold_query_packet)
    _build_cached = staticmethod(_build_cached_query_packet)
    _join_packets = staticmethod(_join_pipeline_packets)
    _batch_decode = False
    _field_tape_type: Any = None
    _compile_decoder_plan: Any = None
    _decode_tape: Any = None
    #: Optional backend hook that decodes rows into a caller-supplied
    #: destination instead of Records. The destination is opaque here.
    _decode_dest: Any = None

    max_queued_operations = 256
    max_emitted_operations = 64
    max_outbound_batch = 256 * 1024
    _eager_flush_idle = True
    #: Capacity of the per-connection async-notification ring (see module docs).
    notify_ring_capacity = _NOTIFY_RING_CAPACITY

    __slots__ = (
        "_backend_key",
        "_backend_pid",
        "_background_tasks",
        "_closed",
        "_completed",
        "_current",
        "_emitted",
        "_failure",
        "_flush_handle",
        "_idle_event",
        "_info",
        "_listen_channels",
        "_loop",
        "_notifications",
        "_notifications_dropped",
        "_notify_event",
        "_pending_closes",
        "_plan_costs",
        "_plans",
        "_plans_bytes",
        "_reader",
        "_reader_task",
        "_register_operations",
        "_sequence",
        "_statement_cache_bytes",
        "_statement_cache_size",
        "_statement_id",
        "_transaction_barrier",
        "_transaction_status",
        "_waiting",
        "_waiting_live",
        "_write_blocked",
        "_write_count",
        "_write_with_backpressure",
        "_writer",
    )

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        info: _ConnectInfo,
        backend_pid: int,
        backend_key: int,
        *,
        statement_cache_size: int = 100,
        statement_cache_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._register_operations = getattr(reader, "register_operations", None)
        self._write_with_backpressure = getattr(writer, "write_with_backpressure", None)
        self._info = info
        self._backend_pid = backend_pid
        self._backend_key = backend_key
        self._loop = asyncio.get_running_loop()
        # Access-ordered so the least-recently-used automatic plan is evicted
        # first; bounded by statement_cache_size to cap per-connection (and,
        # via the Close on eviction, backend) prepared-statement memory.
        self._plans: OrderedDict[str, Plan] = OrderedDict()
        self._plan_costs: dict[str, int] = {}
        self._plans_bytes = 0
        self._statement_cache_size = statement_cache_size
        self._statement_cache_bytes = statement_cache_bytes
        # Statement names retired by eviction, closed on the wire (Close 'S')
        # ahead of the next operation once no in-flight operation still uses them.
        self._pending_closes: list[bytes] = []
        self._statement_id = 0
        self._sequence = 0
        self._waiting: deque[Operation] = deque()
        #: Entries in `_waiting` still in state "waiting". A cancelled operation
        #: is tombstoned in place rather than removed, because `deque.remove`
        #: scans from the left: cancelling the *newest* of k queued operations
        #: walked all k, and nested `async with` scopes cancel newest-first, so
        #: a disconnect or timeout cascade paid O(k^2). Measured at k=4000,
        #: newest-first: 62.9-71.2 ms before, 1.4-1.6 ms after. Every test of
        #: "is there work" must read this and not `len(self._waiting)`, or a
        #: queue of pure tombstones reschedules the flush forever.
        self._waiting_live = 0
        self._emitted: deque[Operation] = deque()
        self._completed: deque[Operation] = deque()
        self._current: Operation | None = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._flush_handle: asyncio.Handle | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._transaction_status = b"I"
        self._transaction_barrier = False
        self._failure: PostgresError | None = None
        self._write_blocked = False
        self._write_count = 0
        self._closed = False
        # LISTEN/NOTIFY state. ``_listen_channels`` keeps the reader task alive
        # while non-empty (so async notifications are drained even when no
        # operation is in flight); notifications land in the bounded ring and
        # wake any ``notifications()`` iterator via the event.
        self._listen_channels: set[str] = set()
        self._notifications: deque[Notification] = deque()
        self._notifications_dropped = 0
        self._notify_event = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped_notifications(self) -> int:
        """Async notifications discarded because the ring overflowed."""
        return self._notifications_dropped

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Wake any notifications() iterator so it observes the close and exits
        # rather than blocking forever on a connection that will never notify.
        self._notify_event.set()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        current_task = asyncio.current_task()
        if self._reader_task is not None and self._reader_task is not current_task:
            self._reader_task.cancel()
        for task in tuple(self._background_tasks):
            if task is not current_task:
                task.cancel()
        self._resolve_pending(InterfaceError("connection is closed"))
        if not self._writer.is_closing():
            self._writer.write(_message(b"X"))
            with contextlib.suppress(ConnectionError):
                await self._writer.drain()
            self._writer.close()
            with contextlib.suppress(ConnectionError):
                await self._writer.wait_closed()
        self._plans.clear()
        self._plan_costs.clear()
        self._plans_bytes = 0

    async def listen(self, channel: str) -> None:
        """Subscribe this connection to asynchronous ``NOTIFY`` on ``channel``.

        Issues ``LISTEN`` and registers interest so the reader task stays alive
        while idle and routes incoming notifications into the ring. Notifications
        are delivered through ``notifications()``. A dedicated connection should
        be held for listening: leasing it back to a pool between notifications
        would stop draining them (that lifecycle is the caller's concern).
        """
        name = _validate_channel(channel)
        # Register before issuing the SQL: LISTEN flushes an operation which
        # starts the reader, and a non-empty channel set keeps that reader from
        # exiting when the operation drains.
        self._listen_channels.add(name)
        try:
            await self.execute(f'LISTEN "{name}"')
        except BaseException:
            self._listen_channels.discard(name)
            raise

    async def unlisten(self, channel: str) -> None:
        """Stop listening on ``channel`` (issues ``UNLISTEN``).

        The channel is deregistered immediately; if it was the last one the
        idle reader may stay parked on the socket until the next message or
        ``close()`` before it observes the empty channel set and exits.
        """
        name = _validate_channel(channel)
        self._listen_channels.discard(name)
        await self.execute(f'UNLISTEN "{name}"')

    async def notifications(self) -> AsyncIterator[Notification]:
        """Yield captured ``NOTIFY`` messages, awaiting new ones when drained.

        The iterator ends when the connection closes. It never blocks while the
        ring is non-empty, so a burst is delivered without interleaved awaits.
        """
        while True:
            while self._notifications:
                yield self._notifications.popleft()
            if self._closed:
                return
            # Clear then re-check to close the race with an enqueue that set the
            # event between the drain above and here.
            self._notify_event.clear()
            if self._notifications:
                continue
            await self._notify_event.wait()

    def _enqueue_notification(self, payload: bytes) -> None:
        notification = _parse_notification(payload)
        if notification is None:
            return
        ring = self._notifications
        if len(ring) >= self.notify_ring_capacity:
            ring.popleft()
            self._notifications_dropped += 1
        ring.append(notification)
        if not self._notify_event.is_set():
            self._notify_event.set()

    async def execute(self, sql: str, *args: object) -> str:
        return await self._submit("execute", sql, args)

    async def fetch(self, sql: str, *args: object) -> list[Record]:
        return await self._submit("fetch", sql, args)

    async def fetchrow(self, sql: str, *args: object) -> Record | None:
        return await self._submit("fetchrow", sql, args)

    async def fetchval(self, sql: str, *args: object) -> object:
        return await self._submit("fetchval", sql, args)

    async def map(
        self,
        method: ResultMode,
        statement: object,
        argument_sets: Any,
        *,
        max_in_flight: int = 32,
    ) -> list[Any]:
        """Run one ``method`` operation per argument set, preserving input order.

        Every input produces a distinct extended-protocol operation with its own
        ``Sync``; duplicate inputs run twice and are never coalesced into an
        ``IN (...)`` or deduplicated. Results come back in input order. At most
        ``max_in_flight`` operations are queued at once, so a generator input
        keeps pipeline depth and retained results bounded. Inside an explicit
        transaction the driver already forbids concurrent operations, so the
        window collapses to one and the operations run strictly in order.

        ``statement`` may be a SQL string or any object exposing ``.sql``.
        """
        if method not in ("execute", "fetch", "fetchrow", "fetchval"):
            raise ValueError(f"unsupported map method: {method!r}")
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be >= 1")
        sql = getattr(statement, "sql", statement)
        if not isinstance(sql, str):
            raise InterfaceError("map statement must be SQL text or expose .sql")
        in_transaction = self._transaction_status != b"I" or self._transaction_barrier
        window = 1 if in_transaction else max_in_flight

        results: list[Any] = []
        in_flight: deque[asyncio.Future[Any]] = deque()
        arguments = iter(argument_sets)
        exhausted = False
        try:
            while True:
                while len(in_flight) < window and not exhausted:
                    try:
                        args = next(arguments)
                    except StopIteration:
                        exhausted = True
                        break
                    in_flight.append(
                        asyncio.ensure_future(self._submit(method, sql, tuple(args)))
                    )
                if not in_flight:
                    break
                # Await in submission order, so results stay input-ordered even
                # though several operations share the pipeline concurrently.
                results.append(await in_flight.popleft())
        except BaseException:
            for pending in in_flight:
                pending.cancel()
            for pending in in_flight:
                with contextlib.suppress(BaseException):
                    await pending
            raise
        return results

    def transaction(self) -> _Transaction:
        """An explicit transaction scope over this connection.

        ``async with connection.transaction() as tx:`` issues ``BEGIN`` on entry
        and ``COMMIT`` on clean exit, or ``ROLLBACK`` if the body raises. Reads
        and writes issued through ``tx`` run in order, so a read group completes
        before a dependent write group begins. If synchronization cannot be
        proven on rollback, the connection is discarded rather than reused.
        """
        return _Transaction(self)

    async def _fetch_into(self, sql: str, args: tuple[object, ...], dest: object) -> Any:
        """Execute ``sql`` and decode its rows into ``dest``.

        ``dest`` is opaque: it is passed to the backend's ``_decode_dest`` hook
        unchanged. Only a backend that installs that hook supports this.
        """
        if self._decode_dest is None:
            raise InterfaceError(
                "this PostgreSQL backend cannot decode into a custom destination"
            )
        return await self._submit("fetch", sql, args, dest=dest)

    async def _submit(
        self,
        mode: ResultMode,
        sql: str,
        args: tuple[object, ...],
        dest: object | None = None,
    ) -> Any:
        if self._closed:
            raise InterfaceError("connection is closed")
        if not isinstance(sql, str) or not sql:
            raise InterfaceError("SQL must be a non-empty string")
        outstanding = self._waiting_live + len(self._emitted)
        transaction_sql = _is_transaction_sql(sql)
        if (self._transaction_status != b"I" or self._transaction_barrier) and outstanding:
            raise InterfaceError("explicit transactions reject concurrent operations")
        if transaction_sql and outstanding:
            raise InterfaceError("transaction control cannot enter an active pipeline")
        if outstanding >= self.max_queued_operations:
            raise PipelineFullError("PostgreSQL pipeline is full")

        self._sequence += 1
        future: asyncio.Future[Any] = self._loop.create_future()
        operation = self._operation_type(
            self._sequence, sql, args, mode, future, None
        )
        operation.dest = dest
        plan = self._plans.get(sql)
        if plan is not None:
            self._plans.move_to_end(sql)  # most-recently-used
        operation.plan = plan
        operation.cold = plan is None
        if plan is None:
            self._statement_id += 1
            operation.statement_name = f"wreath_{self._statement_id}".encode()
            operation.parameter_oids = tuple(_infer_oid(value) for value in args)
            if dest is None:
                operation.packet = self._build_cold(
                    operation.statement_name,
                    sql,
                    args,
                    operation.parameter_oids,
                    mode,
                )
            else:
                # A catalog destination decodes binary only, and a cold operation
                # otherwise binds text -- so the *first* catalog read on any
                # connection failed, and failed as a hang. See
                # `_build_cold_query_packet` for why the format has to be decided
                # here rather than caught downstream.
                #
                # Deliberately the Python builder even when the C one is bound:
                # the accelerated `_build_cold` takes five positional arguments
                # and adding a sixth means moving both twins under the
                # byte-for-byte parity contract to serve a path that runs once
                # per connection, off the hot path, only for migration reads.
                # The bytes are identical bar the result-format code.
                operation.packet = _build_cold_query_packet(
                    operation.statement_name,
                    sql,
                    args,
                    operation.parameter_oids,
                    mode,
                    binary_results=True,
                )
        else:
            operation.statement_name = plan.statement_name
            operation.parameter_oids = plan.parameter_oids
            operation.result_names = plan.result_names
            operation.result_oids = plan.result_oids
            if self._batch_decode:
                operation.decoder_plan = getattr(plan, "decoder_plan", None)
                if plan.result_oids:
                    operation.field_tape = self._field_tape_type(len(plan.result_oids))
            else:
                operation.result_formats = (1,) * len(plan.result_oids)
            operation.packet = self._build_cached(plan, args, mode)
        closes = self._closes_prefix()
        if closes:
            # Retire evicted server statements within this operation's Sync, so
            # the Close is ordered and acknowledged rather than injected loose.
            operation.packet = closes + operation.packet
        if len(operation.packet) > self.max_outbound_batch:
            raise PipelineFullError("operation exceeds maximum outbound batch")

        if transaction_sql:
            self._transaction_barrier = True
        eager = (
            self._eager_flush_idle
            and not self._waiting_live
            and not self._emitted
            and self._flush_handle is None
        )
        # No done-callback: the only cleanup a resolved future needs is the
        # cancellation path, and the `except asyncio.CancelledError` below
        # already runs it (every submitted future is awaited right here, and
        # nothing else holds it). A callback would add one lambda allocation
        # and one call_soon Handle per operation for a duplicate idempotent
        # _cancel_operation call.
        self._waiting.append(operation)
        self._waiting_live += 1
        if eager:
            self._flush()
        elif self._flush_handle is None:
            self._flush_handle = self._loop.call_soon(self._flush)
        try:
            return await future
        except asyncio.CancelledError:
            self._cancel_operation(operation)
            raise

    def _flush(self) -> None:
        self._flush_handle = None
        if self._closed or self._write_blocked or not self._waiting_live:
            return
        available = self.max_emitted_operations - len(self._emitted)
        if available <= 0:
            return
        packets: list[bytes] = []
        operations: list[Operation] = []
        batch_size = 0
        emitted = 0
        while self._waiting and emitted < available:
            operation = self._waiting[0]
            # Tombstones left by `_cancel_operation`. Drained here rather than
            # removed there, and skipped before the batch-size test so a
            # cancelled operation's packet cannot end a batch it will never
            # join.
            if operation.state == "cancelled":
                self._waiting.popleft()
                continue
            if packets and batch_size + len(operation.packet) > self.max_outbound_batch:
                break
            self._waiting.popleft()
            self._waiting_live -= 1
            if operation.future.cancelled():
                operation.state = "cancelled"
                continue
            packets.append(operation.packet)
            operations.append(operation)
            batch_size += len(operation.packet)
            operation.state = "emitted"
            self._emitted.append(operation)
            self._idle_event.clear()
            emitted += 1
        if packets:
            if self._register_operations is not None:
                self._register_operations(tuple(operations))
            payload = packets[0] if len(packets) == 1 else self._join_packets(tuple(packets))
            if self._write_with_backpressure is None:
                self._writer.write(payload)
                pending = self._writer.drain()
            else:
                pending = self._write_with_backpressure(payload)
            self._write_count += 1
            if pending is not None and not (
                isinstance(pending, asyncio.Future) and pending.done()
            ):
                self._write_blocked = True
                task = self._loop.create_task(self._drain(pending))
                self._track_background(task)
            if self._reader_task is None:
                self._reader_task = self._loop.create_task(self._read_pipeline())
        if self._waiting_live and len(self._emitted) < self.max_emitted_operations:
            self._flush_handle = self._loop.call_soon(self._flush)

    async def _drain(self, pending: Awaitable[None]) -> None:
        try:
            await pending
        except (ConnectionError, OSError) as error:
            self._fail_connection(OperationalError("PostgreSQL connection lost"), error)
        finally:
            self._write_blocked = False
            if self._waiting_live and not self._closed and self._flush_handle is None:
                self._flush_handle = self._loop.call_soon(self._flush)

    def _track_background(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _receive_message(self) -> tuple[bytes, bytes]:
        return await _read_message(self._reader)

    async def _read_pipeline(self) -> None:
        try:
            # Stay alive while there are operations in flight OR active LISTEN
            # channels, so asynchronous notifications are drained even when the
            # connection is otherwise idle. Without a listen channel the loop
            # still exits as soon as the pipeline drains.
            while (self._emitted or self._listen_channels) and not self._closed:
                # Publish the head-of-line operation as current BEFORE blocking
                # on the socket: a concurrent cancel must observe an active
                # operation while its query is still running server-side, so it
                # sends a CancelRequest instead of tearing the connection down.
                # (The LISTEN/NOTIFY rewrite briefly moved this below the await,
                # which left _current None mid-query and broke active cancel.)
                self._current = self._emitted[0] if self._emitted else None
                kind, payload = await self._receive_message()
                if not self._emitted:
                    # Idle but listening: only asynchronous messages
                    # (NotificationResponse, NoticeResponse, ParameterStatus)
                    # can legitimately arrive with no operation in flight.
                    self._consume_async_message(kind, payload)
                    continue
                operation = self._emitted[0]
                # Re-publish in case the queue filled while we were parked on the
                # socket with no operation in flight (idle-listening -> emitted).
                self._current = operation
                if kind == b"Z":
                    if len(payload) != 1:
                        raise ProtocolError("invalid ReadyForQuery")
                    self._transaction_status = payload
                    # From here until `_finish_operation` appends it to
                    # `_completed`, `operation` belongs to no queue, and the
                    # work in between (decoding the batch, caching the plan)
                    # can raise. `_current` is what keeps it reachable --
                    # see `_resolve_pending`.
                    self._emitted.popleft()
                    if self._batch_decode and operation.field_tape is not None:
                        self._flush_decode_batch(operation)
                    self._finish_operation(operation)
                    self._current = None
                    if not self._emitted:
                        self._idle_event.set()
                    if self._waiting_live and self._flush_handle is None:
                        self._flush_handle = self._loop.call_soon(self._flush)
                else:
                    self._consume_message(operation, kind, payload)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as error:
            self._fail_connection(OperationalError("PostgreSQL connection lost"), error)
        except PostgresError as error:
            self._fail_connection(error)
        except BaseException as error:  # see below; it re-raises
            # Anything else reaching here is a defect in the driver rather than a
            # wire condition -- a decoder that cannot read the format it was sent,
            # a plan that does not match its rows. It still has to fail every
            # caller before it leaves.
            #
            # This runs in the reader task, which is nobody's awaiter. Without
            # this clause the task died, the `finally` tidied `_reader_task`, and
            # every in-flight operation waited on a future that would never be
            # resolved -- so a driver bug surfaced as a permanent hang. That is
            # exactly how `catalog destination requires binary rows` sat unseen:
            # it only ran under `-m ''`, where it hung the whole suite instead of
            # failing one test.
            #
            # Broad on purpose, and `BaseException` on purpose: a `SystemExit` or
            # a `KeyboardInterrupt` raised in here would strand the same callers.
            # It re-raises, so nothing is swallowed -- the catch exists to resolve
            # the futures, not to survive.
            self._fail_connection(InterfaceError(f"PostgreSQL reader failed: {error}"), error)
            raise
        finally:
            self._reader_task = None
            self._current = None

    def _consume_async_message(self, kind: bytes, payload: bytes) -> None:
        """Route a message received while idle but listening.

        NotificationResponse is captured into the ring; NoticeResponse and
        ParameterStatus are ignored exactly as they are mid-operation. Any other
        message with no operation in flight means the wire is desynchronised.
        """
        if kind == b"A":
            self._enqueue_notification(payload)
        elif kind in {b"N", b"S"}:
            return
        else:
            raise ProtocolError(f"unexpected asynchronous message {kind!r}")

    def _consume_message(self, operation: Operation, kind: bytes, payload: bytes) -> None:
        if kind == b"t":
            operation.parameter_oids = _parse_parameter_description(payload)
        elif kind == b"T":
            (
                operation.result_names,
                operation.result_oids,
                operation.result_formats,
            ) = _parse_row_description(payload)
            if self._batch_decode and operation.result_oids:
                operation.decoder_plan = self._compile_decoder_plan(
                    operation.result_oids,
                    operation.result_formats,
                    operation.result_names,
                )
                operation.field_tape = self._field_tape_type(
                    len(operation.result_oids)
                )
        elif kind == b"D" and operation.error is None and not operation.discarded:
            if self._batch_decode:
                self._tape_data_row(operation, payload)
                return
            fields = _data_fields(payload)
            formats = operation.result_formats or (
                (1,) * len(fields) if not operation.cold else (0,) * len(fields)
            )
            if operation.mode == "fetchval":
                if not operation.have_value and fields:
                    operation.one_value = self._decode(
                        operation.result_oids[0], formats[0], fields[0]
                    )
                    operation.have_value = True
            elif operation.mode == "fetchrow":
                if operation.one_row is None:
                    values = tuple(
                        self._decode(oid, fmt, field)
                        for oid, fmt, field in zip(
                            operation.result_oids, formats, fields, strict=True
                        )
                    )
                    operation.one_row = self._record_type(operation.result_names, values)
            elif operation.mode == "fetch":
                values = tuple(
                    self._decode(oid, fmt, field)
                    for oid, fmt, field in zip(
                        operation.result_oids, formats, fields, strict=True
                    )
                )
                assert operation.rows is not None
                operation.rows.append(self._record_type(operation.result_names, values))
        elif kind == b"C":
            operation.command = payload.rstrip(b"\x00").decode("utf-8", "replace")
        elif kind == b"E":
            operation.error = _parse_error(payload)
        # b"A" is NotificationResponse: an asynchronous NOTIFY that may be
        # interleaved into any operation's message stream. Capture it into the
        # per-connection ring instead of discarding it, so notifications sent
        # while an operation happens to be in flight are not lost.
        elif kind == b"A":
            self._enqueue_notification(payload)
        # b"S" is ParameterStatus (b"s" is PortalSuspended -- different message).
        # The server may send it at any time, not just during startup: any
        # GUC_REPORT setting that changes mid-session reports itself, and
        # `SET default_transaction_read_only = on` -- which Database._open runs
        # for every read pool -- is one of them from PostgreSQL 14 on. Ignored
        # here for the same reason as NoticeResponse: it is asynchronous and
        # carries nothing this operation asked for.
        elif kind in {b"1", b"2", b"3", b"n", b"s", b"S", b"N"} or (
            kind == b"D" and (operation.error is not None or operation.discarded)
        ):
            # b"3" is CloseComplete, acknowledging a prepared-statement Close
            # piggybacked onto this operation when an LRU plan was evicted.
            return
        else:
            raise ProtocolError(f"unexpected backend message {kind!r}")

    def _tape_data_row(self, operation: Operation, payload: bytes) -> None:
        if operation.mode == "execute" or operation.field_tape is None:
            return
        if operation.mode in {"fetchval", "fetchrow"} and operation.field_tape.row_count:
            return
        selected = 1 if operation.mode == "fetchval" else len(operation.result_oids)
        operation.field_tape.append(payload, selected)
        if operation.mode == "fetch" and operation.field_tape.row_count >= 256:
            self._flush_decode_batch(operation)

    def _flush_decode_batch(self, operation: Operation) -> None:
        tape = operation.field_tape
        if tape is None or tape.row_count == 0:
            return
        if operation.error is not None or operation.discarded:
            tape.clear()
            return
        if operation.decoder_plan is None:
            raise ProtocolError("DataRow received without a decoder plan")
        if operation.dest is not None:
            assert operation.rows is not None
            self._decode_dest(
                operation.decoder_plan, tape, operation.dest, 256, operation.rows
            )
            return
        decoded = self._decode_tape(
            operation.decoder_plan, tape, operation.mode, 256
        )
        if operation.mode == "fetch":
            assert operation.rows is not None
            operation.rows.extend(decoded)
        elif operation.mode == "fetchrow":
            operation.one_row = decoded
        elif operation.mode == "fetchval":
            operation.one_value = decoded
            operation.have_value = True

    @property
    def prepared_plan_count(self) -> int:
        """Automatic prepared plans currently cached on this connection."""
        return len(self._plans)

    def _closes_prefix(self) -> bytes:
        """Close messages for evicted statements no in-flight operation still
        uses; any still referenced stay pending for a later operation."""
        if not self._pending_closes:
            return b""
        in_flight = {op.statement_name for op in self._emitted}
        closeable = [name for name in self._pending_closes if name not in in_flight]
        if not closeable:
            return b""
        self._pending_closes = [name for name in self._pending_closes if name in in_flight]
        return b"".join(_message(b"C", b"S" + _cstring(name)) for name in closeable)

    def _finish_operation(self, operation: Operation) -> None:
        operation.state = "completed"
        if operation.cold and operation.error is None:
            plan = self._plan_type(
                operation.statement_name,
                operation.parameter_oids,
                operation.result_oids,
                operation.result_names,
            )
            retained = _plan_retained_bytes(operation.sql, plan)
            self._plans[operation.sql] = plan
            self._plan_costs[operation.sql] = retained
            self._plans_bytes += retained
            self._plans.move_to_end(operation.sql)
            while (
                len(self._plans) > self._statement_cache_size
                or self._plans_bytes > self._statement_cache_bytes
            ):
                evicted_sql, evicted = self._plans.popitem(last=False)
                self._plans_bytes -= self._plan_costs.pop(evicted_sql)
                self._pending_closes.append(evicted.statement_name)
        if self._transaction_barrier and _is_transaction_sql(operation.sql):
            self._transaction_barrier = False
        self._completed.append(operation)
        self._publish_completed()

    def _publish_completed(self) -> None:
        while self._completed:
            operation = self._completed.popleft()
            future = operation.future
            if future.done() or operation.discarded:
                continue
            if operation.error is not None:
                future.set_exception(operation.error)
            elif operation.mode == "execute":
                future.set_result(operation.command)
            elif operation.mode == "fetch":
                future.set_result(operation.rows)
            elif operation.mode == "fetchrow":
                future.set_result(operation.one_row)
            else:
                future.set_result(operation.one_value)

    def _cancel_operation(self, operation: Operation) -> None:
        if operation.state in {"completed", "cancelled"} or operation.discarded:
            return
        if operation.state == "waiting":
            # Tombstone, never `self._waiting.remove(operation)`: that scans
            # from the left, so cancelling the newest of k queued operations
            # walked all k. `_flush` drops it when it reaches the head.
            self._waiting_live -= 1
            operation.state = "cancelled"
            if _is_transaction_sql(operation.sql):
                self._transaction_barrier = False
        elif operation.state == "emitted":
            operation.discarded = True
            if self._current is operation:
                if self._backend_pid and self._backend_key:
                    task = self._loop.create_task(self._send_cancel_request())
                    self._track_background(task)
                else:
                    self._fail_connection(
                        OperationalError("cannot safely cancel active PostgreSQL operation")
                    )

    async def _send_cancel_request(self) -> None:
        packet = struct.pack(
            "!IIII", 16, 80877102, self._backend_pid, self._backend_key
        )
        try:
            if self._info.unix:
                _, writer = await asyncio.open_unix_connection(self._info.host)
            else:
                _, writer = await asyncio.open_connection(
                    self._info.host, int(self._info.port)
                )
            writer.write(packet)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except (OSError, ConnectionError) as error:
            self._fail_connection(
                OperationalError("PostgreSQL cancellation state is uncertain"), error
            )

    def _fail_connection(
        self, error: PostgresError, cause: BaseException | None = None
    ) -> None:
        if self._closed:
            return
        self._closed = True
        self._failure = error
        if cause is not None:
            error.__cause__ = cause
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._resolve_pending(error)
        self._idle_event.set()
        self._writer.close()

    def _resolve_pending(self, error: PostgresError) -> None:
        """Fail every operation this connection could still have resolved.

        The three queues do not account for all of them. An operation moves
        between queues without ever being in two at once, and the reader hands
        it across those seams *while running code that can raise* -- decoding a
        batch, caching a plan. `_read_pipeline` pops the head of `_emitted`
        before decoding it and only then appends it to `_completed`, so a decode
        failure left the operation in no queue at all: the sweep could not see
        it, its future was never resolved, and the caller waited forever. The
        `except BaseException` in the reader was already calling this and still
        did not save that caller, because the operation had fallen through the
        gap between two of the containers it was searching.

        `_current` closes the gap. The reader publishes the head-of-line
        operation there before it touches the socket and clears it only once the
        operation is finished, so it names whatever is in transit no matter
        which queue currently holds it (or none). Sweeping it costs one extra
        entry that is usually a duplicate of `_emitted[0]`, and the `done()`
        guard makes the duplicate free.
        """
        for operation in (
            self._current,
            *self._waiting,
            *self._emitted,
            *self._completed,
        ):
            if operation is not None and not operation.future.done():
                operation.future.set_exception(error)
        self._waiting.clear()
        self._waiting_live = 0
        self._emitted.clear()
        self._completed.clear()
        self._current = None


def _is_transaction_sql(sql: str) -> bool:
    first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    return first in {"BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


async def _authenticate(
    reader: Any,
    writer: asyncio.StreamWriter,
    info: _ConnectInfo,
) -> tuple[int, int]:
    backend_pid = 0
    backend_key = 0
    params = b"user\x00" + _cstring(info.user) + b"database\x00" + _cstring(info.database)
    params += b"client_encoding\x00UTF8\x00\x00"
    startup = struct.pack("!II", len(params) + 8, 196608) + params
    writer.write(startup)
    await writer.drain()
    scram_state: _ScramState | None = None
    expected_signature: str | None = None
    while True:
        kind, payload = await _read_message(reader)
        if kind == b"R":
            if len(payload) < 4:
                raise ProtocolError("truncated Authentication request")
            method = struct.unpack_from("!I", payload)[0]
            auth_data = payload[4:]
            if method == 0:
                continue
            if method == 3:
                if info.password is None:
                    raise OperationalError("password required")
                writer.write(_message(b"p", _cstring(info.password)))
                await writer.drain()
            elif method == 10:
                mechanisms = auth_data.rstrip(b"\x00").split(b"\x00")
                if b"SCRAM-SHA-256" not in mechanisms or info.password is None:
                    raise OperationalError("SCRAM-SHA-256 authentication unavailable")
                scram_state, first = _scram_start(info.user)
                initial = b"SCRAM-SHA-256\x00" + struct.pack("!I", len(first)) + first.encode()
                writer.write(_message(b"p", initial))
                await writer.drain()
            elif method == 11:
                if scram_state is None or info.password is None:
                    raise ProtocolError("unexpected SASL continue")
                final, expected_signature = _scram_continue(
                    scram_state, info.password, auth_data.decode()
                )
                writer.write(_message(b"p", final.encode()))
                await writer.drain()
            elif method == 12:
                if expected_signature is None:
                    raise ProtocolError("unexpected SASL final")
                _scram_finish(expected_signature, auth_data.decode())
            else:
                raise OperationalError(f"unsupported PostgreSQL authentication method {method}")
        elif kind == b"E":
            raise _parse_error(payload)
        elif kind == b"Z":
            return backend_pid, backend_key
        elif kind == b"K":
            if len(payload) != 8:
                raise ProtocolError("invalid BackendKeyData")
            backend_pid, backend_key = struct.unpack("!II", payload)
        elif kind in {b"S", b"N"}:
            continue
        else:
            raise ProtocolError(f"unexpected startup message {kind!r}")


async def _connect_with_type(
    dsn: str,
    connection_type: type[Connection],
    *,
    statement_cache_size: int = 100,
    statement_cache_bytes: int = 4 * 1024 * 1024,
) -> Connection:
    info = _parse_dsn(dsn)
    try:
        if info.unix:
            reader, writer = await asyncio.open_unix_connection(info.host)
        else:
            reader, writer = await asyncio.open_connection(info.host, int(info.port))
        backend_pid, backend_key = await _authenticate(reader, writer, info)
    except PostgresError:
        if "writer" in locals():
            writer.close()
        raise
    except (OSError, asyncio.IncompleteReadError, ConnectionError) as error:
        if "writer" in locals():
            writer.close()
        raise OperationalError("could not connect to PostgreSQL") from error
    return connection_type(
        reader, writer, info, backend_pid, backend_key,
        statement_cache_size=statement_cache_size,
        statement_cache_bytes=statement_cache_bytes,
    )


class _BufferedStartupReader:
    __slots__ = ("_buffer", "_protocol")

    def __init__(self, protocol: Any) -> None:
        self._protocol = protocol
        self._buffer = bytearray()

    async def readexactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            kind, payload = await self._protocol.read_message()
            payload_bytes = bytes(payload)
            self._buffer += kind
            self._buffer += struct.pack("!I", len(payload_bytes) + 4)
            self._buffer += payload_bytes
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result


async def _connect_buffered(
    dsn: str,
    connection_type: type[Connection],
    protocol_type: type[Any],
    *,
    statement_cache_size: int = 100,
    statement_cache_bytes: int = 4 * 1024 * 1024,
) -> Connection:
    info = _parse_dsn(dsn)
    loop = asyncio.get_running_loop()
    protocol = protocol_type()
    if info.unix:
        await loop.create_unix_connection(lambda: protocol, info.host)
    else:
        await loop.create_connection(lambda: protocol, info.host, int(info.port))
    try:
        backend_pid, backend_key = await _authenticate(
            _BufferedStartupReader(protocol), protocol, info
        )
    except BaseException:
        protocol.close()
        raise
    return connection_type(
        protocol,
        protocol,
        info,
        backend_pid,
        backend_key,
        statement_cache_size=statement_cache_size,
        statement_cache_bytes=statement_cache_bytes,
    )


async def connect(
    dsn: str,
    *,
    statement_cache_size: int = 100,
    statement_cache_bytes: int = 4 * 1024 * 1024,
) -> Connection:
    """Open one asynchronous PostgreSQL connection."""

    return await _connect_with_type(
        dsn,
        Connection,
        statement_cache_size=statement_cache_size,
        statement_cache_bytes=statement_cache_bytes,
    )


__all__ = [
    "Connection",
    "InterfaceError",
    "OperationalError",
    "PipelineFullError",
    "PostgresError",
    "ProtocolError",
    "Record",
    "connect",
]
