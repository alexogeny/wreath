"""The wire-plan vocabulary: kind codes, flag bits, and the decode error.

A plan is compiled from a declaration at *class creation* -- once, in Python, by
`wreath.protobuf._resolve` -- and only then handed to the codec. So two parties
read these integers: the declaration compiler, which is Python and has no C
counterpart, and `_native/protobuf.c`, which switches on the same numbers. They
live here so neither owns them.

One plan row is `(number, kind, flags, subplan)`, all plain ints except
`subplan`, so a plan crosses into C without conversion:

    number   1..536870911, already validated by the declaration layer
    kind     one of the KIND_* codes below
    flags    FLAG_* bits
    subplan  a nested plan tuple for KIND_MESSAGE / map fields, else None

`ProtobufDecodeError` lives here so the declaration facade and wire codec name
the same refusal without keeping mutable interpreter objects in native global
state.
"""

from __future__ import annotations

__all__ = [
    "FLAG_MAP",
    "FLAG_OPTIONAL",
    "FLAG_PACKED",
    "FLAG_REPEATED",
    "KIND_BOOL",
    "KIND_BYTES",
    "KIND_DOUBLE",
    "KIND_ENUM",
    "KIND_FIXED32",
    "KIND_FIXED64",
    "KIND_FLOAT",
    "KIND_INT32",
    "KIND_INT64",
    "KIND_MESSAGE",
    "KIND_SFIXED32",
    "KIND_SFIXED64",
    "KIND_SINT32",
    "KIND_SINT64",
    "KIND_STRING",
    "KIND_UINT32",
    "KIND_UINT64",
    "ProtobufDecodeError",
]

# Stable integer codes: `_native/protobuf.c` switches on these, so the numbering
# is part of the contract between the compiler and the codec.

KIND_INT32 = 1
KIND_INT64 = 2
KIND_UINT32 = 3
KIND_UINT64 = 4
KIND_SINT32 = 5
KIND_SINT64 = 6
KIND_BOOL = 7
KIND_ENUM = 8
KIND_FIXED64 = 9
KIND_SFIXED64 = 10
KIND_DOUBLE = 11
KIND_FIXED32 = 12
KIND_SFIXED32 = 13
KIND_FLOAT = 14
KIND_STRING = 15
KIND_BYTES = 16
KIND_MESSAGE = 17

FLAG_REPEATED = 1
FLAG_PACKED = 2
FLAG_OPTIONAL = 4
FLAG_MAP = 8


class ProtobufDecodeError(ValueError):
    """A buffer could not be parsed as the declared message.

    Always raised by name rather than allowed to surface as an `IndexError` or a
    `UnicodeDecodeError`: this codec sits in front of bytes a peer controls, and
    a caller needs one exception type to catch and turn into a 400.
    """
