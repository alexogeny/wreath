# Protocol Buffers

Most of the time JSON is the right answer, and Wreath treats it as the default
everywhere. But sometimes the other end of the wire is not a browser: a mobile
client paying for every byte, a sensor on a metered satellite link, an OTLP
receiver, or a service written in another language whose contract already exists
as a `.proto` file. For those, `wreath.protobuf` speaks the format directly, with
no third-party package and no code generation step.

It follows the same shape as the rest of the framework. You declare, Wreath
compiles the declaration once at startup, and the request path walks a flat plan
instead of re-deriving anything.

## A message is a class

```python
from wreath.protobuf import decode, encode, field, message

@message
class Position:
    collar_id: int = field(1)
    lat: float = field(2)
    lon: float = field(3)
    accuracy_m: float | None = field(4)

raw = encode(Position(collar_id=7, lat=-33.8, lon=151.2))
back = decode(Position, raw)
```

`@message` makes the class a dataclass, so equality, `repr` and construction all
behave the way you already expect.

### Field numbers are written down, never inferred

The number in `field(1)` is the contract with every peer you will ever have. It
is spelled out rather than taken from declaration order, because otherwise
tidying the class — moving a field up, sorting alphabetically — would silently
change what every existing client reads. Reordering the class above changes
nothing on the wire.

Wreath refuses a duplicate number, a number outside `1..536870911`, and anything
inside the `19000..19999` range the specification reserves for itself. All three
are refused when the class is created, naming the field.

### Presence: `int` and `int | None` mean different things

proto3 has two kinds of presence, and the annotation picks between them.

A plain `int` has *implicit* presence: zero is the default, and a field holding
its default is not written at all. `encode(Position(collar_id=0, ...))` omits
`collar_id` entirely, and a decoder that never sees it produces `0`. Absent and
zero are the same thing, which is exactly what proto3 intends.

`int | None` has *explicit* presence: `None` means absent, and any value —
including `0` — is written. Use it when "unset" and "zero" are genuinely
different answers, as `accuracy_m` above.

Nested messages always have explicit presence, because there is no zero message
that could stand in for absent.

### Narrowing the wire type

`int` defaults to `int64`, which is always correct and sometimes wasteful. When
you know more, say so:

```python
@message
class Reading:
    delta: int = field(1, kind="sint32")     # small, often negative
    checksum: int = field(2, kind="fixed64")  # uniformly distributed
```

`sint32` and `sint64` zigzag, so a small negative number stays one byte instead
of ten. `fixed32`/`fixed64` skip the varint entirely, which wins for hashes and
anything uniformly distributed. A `kind` that does not fit the annotation is
refused at import.

### Repeated, maps and oneof

```python
@message
class Track:
    samples: list[int] = field(1)                 # packed by default
    tags: list[str] = field(2)                    # never packed
    counts: dict[str, int] = field(3)
    detail: str | None = field(4, oneof="body")
    blob: bytes | None = field(5, oneof="body")
```

Repeated scalars pack into a single length-delimited field, which is proto3's
default and almost always what you want; pass `packed=False` if a peer needs the
older form. Wreath's decoder accepts *both* representations regardless of what
you declared, because a peer built against an older schema may send either.

A `oneof` group holds at most one member. Setting one on decode clears the
others, and when a malformed message carries two, the last on the wire wins —
the behaviour the specification requires. Every member must be optional.

## Unknown fields are preserved, and that is deliberate

Elsewhere Wreath is strict: an unexpected field in a JSON body is rejected,
because a name is human-authored and an unexpected one is usually a typo worth
catching.

Protobuf is a different situation and gets the opposite treatment. A field
*number* is an allocated, deliberate part of a contract, and encountering one you
do not know means the peer is newer than you — not that anything is wrong.
Tolerating that is the entire mechanism by which protobuf lets a fleet upgrade
one service at a time.

So Wreath captures unknown fields verbatim and writes them back out:

```python
relayed = encode(decode(Position, raw_from_a_newer_peer))
assert relayed == raw_from_a_newer_peer
```

A service in the middle of your estate can decode, inspect, re-encode and forward
a message without destroying data it was never taught about. `unknown_fields(msg)`
returns those bytes if you want to look.

Enums get the same treatment: a value your build does not know survives as the
integer the peer sent, rather than being lost or forced to zero.

## What it refuses, and why

This codec exists to read bytes from someone you do not control, so its refusals
matter as much as its round trips. Every one of them raises
`ProtobufDecodeError`, so there is a single thing to catch and turn into a 400:

- a **truncated buffer**, or a length prefix claiming more bytes than remain
- a **varint longer than ten bytes** — the most a 64-bit value can occupy, and an
  unbounded run of continuation bytes is how a decoder walks off the end
- **field number zero**, which no valid message contains
- **group wire types**, deprecated since proto2 and refused by name rather than
  guessed at
- **invalid UTF-8** in a string field

On the way out, a value that does not fit its declared kind — `-1` in a
`uint32`, `2**31` in an `int32` — raises rather than truncating into something a
peer would read as a different number.

## What this is not

`wreath.protobuf` is a codec, not an implementation of protobuf. It speaks the
wire format for declarations written in Python. It deliberately does not have
descriptors, reflection, `Any`, dynamic messages, or a runtime `.proto` parser,
and it does not implement the canonical protobuf-JSON mapping. Declarations are
Python here for the same reason schemas, routes and validators are.

If you need any of that, you need the `protobuf` package and a code-generation
step, and that is a reasonable thing to want — it is just a different tool.

## Native and pure

Like JSON and MessagePack, the codec ships twice: `src/wreath/_pure/protobuf.py`
is the reference implementation, and `src/wreath/_native/protobuf.c` is a faster
twin of it. `tests/test_protobuf_parity.py` holds the two byte-for-byte over a
corpus that walks every width transition, and `WREATH_PURE=1` selects the
reference if you ever need to rule the C out.

Reference: [`wreath.protobuf`](../reference/protobuf.md).
