from __future__ import annotations

import re
from base64 import b64encode
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ._native import _core

__all__ = [
    "Date",
    "DisplayString",
    "Item",
    "StructuredFieldError",
    "Token",
    "parse_boolean_item",
    "parse_dictionary",
    "serialize_dictionary",
    "serialize_item",
    "serialize_list",
]

_KEY = re.compile(r"[a-z*][a-z0-9_.*-]*\Z")
_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~:/A-Za-z0-9]*\Z")
_INTEGER_LIMIT = 999_999_999_999_999
_PARAMETER_KEY = rb"[a-z*][a-z0-9_.*-]*"
_BASE64 = (
    rb"(?:[A-Za-z0-9+/]{4})*"
    rb"(?:[A-Za-z0-9+/]{2}(?:==)?|[A-Za-z0-9+/]{3}=?)?"
)
_PARAMETER_VALUE = (
    rb"(?:\?[01]|-?(?:\d{1,12}\.\d{1,3}|\d{1,15})|@-?\d{1,15}|"
    rb"[A-Za-z*][!#$%&'*+\-.^_`|~:/A-Za-z0-9]*|:" + _BASE64 + rb":|"
    rb'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\["\\])*"|'
    rb'%"(?:%[0-9a-f]{2}|[\x20-\x21\x23-\x24\x26-\x7e])*"'
    rb")"
)
_BOOLEAN_ITEM = re.compile(
    rb"\?([01])(?:; *" + _PARAMETER_KEY + rb"(?:=" + _PARAMETER_VALUE + rb")?)*\Z"
)
_DISPLAY_PARAMETER = re.compile(rb'=%"([^"]*)"')


class StructuredFieldError(ValueError):
    """An RFC 9651 field value could not be parsed."""


class Token(str):
    def __new__(cls, value: str) -> Token:
        if not isinstance(value, str):
            raise TypeError(f"structured token must be str, got {type(value).__name__}")
        if _TOKEN.fullmatch(value) is None:
            raise ValueError(f"invalid structured token {value!r}")
        return str.__new__(cls, value)


@dataclass(frozen=True, slots=True)
class Date:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise TypeError(
                f"structured date must be an integer timestamp, got {type(self.value).__name__}"
            )
        if not -_INTEGER_LIMIT <= self.value <= _INTEGER_LIMIT:
            raise ValueError(
                f"structured date {self.value} must be between "
                f"{-_INTEGER_LIMIT} and {_INTEGER_LIMIT}"
            )


@dataclass(frozen=True, slots=True)
class DisplayString:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(
                f"structured display string must be str, got {type(self.value).__name__}"
            )
        try:
            self.value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("structured display string must contain valid Unicode") from error


type BareItem = str | Token | bytes | int | bool | Date | DisplayString


@dataclass(frozen=True, slots=True)
class Item:
    value: BareItem
    parameters: Mapping[str, BareItem] = field(default_factory=dict)


def _serialize_string(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"invalid structured string {value!r}: expected ASCII") from error
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise ValueError(f"invalid structured string {value!r}: expected SP or visible ASCII")
    return b'"' + encoded.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _serialize_display_string(value: DisplayString) -> bytes:
    output = bytearray(b'%"')
    for byte in value.value.encode("utf-8"):
        if 0x20 <= byte <= 0x21 or 0x23 <= byte <= 0x24 or 0x26 <= byte <= 0x7E:
            output.append(byte)
        else:
            output.extend(f"%{byte:02x}".encode("ascii"))
    output.extend(b'"')
    return bytes(output)


def _serialize_bare(value: BareItem) -> bytes:
    if isinstance(value, Date):
        return b"@" + str(value.value).encode("ascii")
    if isinstance(value, DisplayString):
        return _serialize_display_string(value)
    if isinstance(value, Token):
        return value.encode("ascii")
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, bool):
        return b"?1" if value else b"?0"
    if isinstance(value, bytes):
        return b":" + b64encode(value) + b":"
    if isinstance(value, int):
        if not -_INTEGER_LIMIT <= value <= _INTEGER_LIMIT:
            raise ValueError(
                f"structured integer {value} must be between {-_INTEGER_LIMIT} and {_INTEGER_LIMIT}"
            )
        return str(value).encode("ascii")
    raise TypeError(f"unsupported structured item type {type(value).__name__}")


def serialize_item(item: Item) -> bytes:
    output = bytearray(_serialize_bare(item.value))
    output.extend(_serialize_parameters(item.parameters))
    return bytes(output)


def _serialize_parameters(parameters: Mapping[str, BareItem]) -> bytes:
    output = bytearray()
    for name, value in parameters.items():
        if _KEY.fullmatch(name) is None:
            raise ValueError(f"invalid structured parameter name {name!r}")
        output.extend(b";")
        output.extend(name.encode("ascii"))
        if value is not True:
            output.extend(b"=")
            output.extend(_serialize_bare(value))
    return bytes(output)


def serialize_dictionary(members: Mapping[str, Item]) -> bytes:
    if not members:
        raise ValueError("structured dictionary needs at least one member")
    output: list[bytes] = []
    for name, item in members.items():
        if _KEY.fullmatch(name) is None:
            raise ValueError(f"invalid structured dictionary member name {name!r}")
        if not isinstance(item, Item):
            raise TypeError(
                f"structured dictionary member {name!r} must be Item, not "
                f"{type(item).__name__}"
            )
        member = bytearray(name.encode("ascii"))
        if item.value is not True:
            member.extend(b"=")
            member.extend(_serialize_bare(item.value))
        member.extend(_serialize_parameters(item.parameters))
        output.append(bytes(member))
    return b", ".join(output)


def serialize_list(items: Iterable[Item]) -> bytes:
    return b", ".join(serialize_item(item) for item in items)


def parse_dictionary(
    value: bytes | str,
    *,
    max_bytes: int = 8192,
    max_members: int = 64,
) -> dict[str, Item]:
    """Parse a bounded RFC 9651 Dictionary of Items.

    The native parser is also the owner used by HTTP Message Signatures. This
    surface turns its internal `(value, parameters)` pairs into the same `Item`
    objects the serializer consumes, so newer fields do not grow independent
    structured-field grammars.
    """
    if isinstance(value, bytes):
        text = value.decode("latin-1")
    elif isinstance(value, str):
        text = value
    else:
        raise TypeError(f"structured dictionary must be bytes or str, got {type(value).__name__}")
    parsed = _core.signature_parse_dictionary(
        text,
        False,
        StructuredFieldError,
        max_bytes,
        max_members,
    )
    return {name: Item(item, parameters) for name, (item, parameters) in parsed.items()}


def parse_boolean_item(value: bytes | str) -> bool | None:
    """Return an RFC 9651 Boolean Item value, or ``None`` when it is invalid."""
    if isinstance(value, str):
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            return None
    elif isinstance(value, bytes):
        encoded = value
    else:
        return None
    match = _BOOLEAN_ITEM.fullmatch(encoded.strip(b" "))
    if match is None or not _valid_display_parameters(encoded):
        return None
    return match[1] == b"1"


def _valid_display_parameters(value: bytes) -> bool:
    for match in _DISPLAY_PARAMETER.finditer(value):
        encoded = match[1]
        decoded = bytearray()
        index = 0
        while index < len(encoded):
            if encoded[index] == 0x25:
                decoded.append(int(encoded[index + 1 : index + 3], 16))
                index += 3
            else:
                decoded.append(encoded[index])
                index += 1
        try:
            decoded.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True
