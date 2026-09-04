"""The proto target: a `.proto` schema for the shapes this API exchanges.

A consumer in another language — a mobile client, an embedded device, a service
written in Go — gets its schema generated from the same `ApiModel` that
produces the OpenAPI document, the TypeScript client and the Python one. The
usual arrangement is the reverse: a hand-maintained `.proto` beside a
hand-maintained REST contract, agreeing by discipline. Here they cannot
disagree, because one model produces both.

**Messages only, deliberately.** Nothing here emits a `service`. Turning REST
operations into RPCs means inventing a request and a response message per
operation and a naming scheme for both, and that invented surface would be a
second contract with nobody to check it against. `wreath.grpc` declares its own
methods against real `@message` classes; that is where an RPC contract belongs.

**Field numbers come from declaration order**, which makes reordering a
dataclass a wire-breaking change. That is stated in the generated header rather
than papered over: `wreath.protobuf` makes numbers explicit for exactly this
reason, and a generator cannot recover an intent the source never recorded.
Pin the schema in review, or declare the message with `wreath.protobuf` and
generate from that instead.

What cannot be expressed is **refused by name** rather than approximated. A
`.proto` that compiles and describes the wrong bytes is worse than one that was
never written, because the failure surfaces in another team's decoder.
"""

from __future__ import annotations

from ..model import ApiModel, Model, TypeRef

#: `TypeRef.kind` -> proto3 scalar. Integers widen to 64-bit: the model does not
#: record a width, and a value that overflows `int32` is a data-loss bug in a
#: consumer we cannot see.
_SCALARS: dict[str, str] = {
    "boolean": "bool",
    "integer": "int64",
    "number": "double",
    "string": "string",
}

#: Named scalars all travel as `string`, matching what the JSON contract and
#: the OpenAPI `format` already say. `bytes` is the one that does not.
_NAMED: dict[str, str] = {
    "date-time": "string",
    "date": "string",
    "uuid": "string",
    "decimal": "string",
    "byte": "bytes",
}


class ProtoTargetError(Exception):
    """A shape this API exchanges cannot be expressed in proto3."""


def _refuse(what: str, where: str) -> ProtoTargetError:
    return ProtoTargetError(
        f"cannot express {what} in proto3 ({where}). The proto target refuses "
        "rather than approximating: a schema that compiles and describes the "
        "wrong bytes fails in a consumer's decoder, where it is hardest to "
        "diagnose."
    )


def _unwrap_optional(ref: TypeRef) -> tuple[TypeRef, bool]:
    """`T | None` -> `(T, True)`. proto3 spells that `optional T`."""
    if ref.kind != "union":
        return ref, False
    members = [a for a in ref.arguments if a.kind != "null"]
    if len(members) != len(ref.arguments) and len(members) == 1:
        return members[0], True
    return ref, False


def _field_type(ref: TypeRef, where: str) -> tuple[str, str]:
    """Return `(prefix, type)` — prefix is `repeated `, `optional ` or empty."""
    ref, optional = _unwrap_optional(ref)
    prefix = "optional " if optional else ""

    if ref.name in _NAMED:
        # No `is not None` guard: `None in _NAMED` is already False, and a
        # second spelling of one check is how two spellings drift apart.
        return prefix, _NAMED[ref.name]
    if ref.kind in _SCALARS:
        return prefix, _SCALARS[ref.kind]
    if ref.kind == "reference":
        if ref.name is None:
            raise _refuse("an unnamed model reference", where)
        return prefix, ref.name
    if ref.kind == "array":
        if not ref.arguments:
            raise _refuse("an untyped array", where)
        inner_prefix, inner = _field_type(ref.arguments[0], where)
        if inner_prefix:
            # `repeated optional T` and `repeated repeated T` are both illegal.
            raise _refuse(f"a repeated {inner_prefix.strip()} value", where)
        return "repeated ", inner
    if ref.kind == "record":
        if not ref.arguments:
            raise _refuse("an untyped mapping", where)
        value_prefix, value = _field_type(ref.arguments[0], where)
        if value_prefix:
            raise _refuse(f"a map of {value_prefix.strip()} values", where)
        return "", f"map<string, {value}>"
    if ref.kind == "coordinate":
        return prefix, "Coordinate"
    if ref.kind == "page":
        # The model builder refuses bare `Page` before a target sees it; every
        # page reference here therefore has exactly one element argument.
        element_prefix, element = _field_type(ref.arguments[0], where)
        if element_prefix:
            raise _refuse(f"a page of {element_prefix.strip()} values", where)
        return prefix, _page_message_name(element)
    if ref.kind == "literal":
        return prefix, _literal_type(ref, where)
    if ref.kind == "unknown":
        raise _refuse("an unannotated value", where)
    if ref.kind == "null":
        raise _refuse("a null-only value", where)
    if ref.kind == "tuple":
        raise _refuse("a heterogeneous tuple", where)
    if ref.kind == "union":
        raise _refuse("a multi-type union (proto3 needs a named `oneof`)", where)
    raise _refuse(f"a {ref.kind!r} value", where)


def _literal_type(ref: TypeRef, where: str) -> str:
    """A literal travels as its underlying scalar, not a generated enum.

    A proto3 enum needs a zero value and a name for every member. Inventing
    both would put names in the contract that the source never chose, and a
    literal whose values are not valid identifiers could not get one at all.
    """
    kinds = {type(value) for value in ref.literals if value is not None}
    if not kinds:
        raise _refuse("an empty literal", where)
    if kinds <= {bool}:
        return "bool"
    if kinds <= {int}:
        return "int64"
    if kinds <= {str}:
        return "string"
    raise _refuse("a literal mixing value types", where)


def _page_message_name(element: str) -> str:
    return f"Page{element[0].upper()}{element[1:]}" if element else "Page"


def _message(model: Model) -> str:
    lines = [f"message {model.name} {{"]
    for number, field in enumerate(model.fields, start=1):
        where = f"{model.name}.{field.wire_name}"
        prefix, kind = _field_type(field.type, where)
        lines.append(f"  {prefix}{kind} {field.wire_name} = {number};")
    lines.append("}")
    return "\n".join(lines)


def _page_messages(api: ApiModel) -> list[str]:
    """A `Page<T>` wrapper per element type actually returned.

    Generic messages do not exist in proto3, so each instantiation gets its own
    message. Only the ones an operation returns are emitted -- an unused
    wrapper is a name in someone's namespace for nothing.
    """
    elements: dict[str, str] = {}

    def walk(ref: TypeRef, where: str) -> None:
        if ref.kind == "page" and ref.arguments:
            _, element = _field_type(ref.arguments[0], where)
            elements.setdefault(element, where)
        for argument in ref.arguments:
            walk(argument, where)

    for operation in api.operations:
        walk(operation.response_body, operation.id)
        if operation.request_body is not None:
            walk(operation.request_body, operation.id)
    for model in api.models:
        for field in model.fields:
            walk(field.type, f"{model.name}.{field.wire_name}")

    return [
        "\n".join(
            [
                f"message {_page_message_name(element)} {{",
                f"  repeated {element} items = 1;",
                "  int64 total = 2;",
                "  int64 page = 3;",
                "  int64 size = 4;",
                "}",
            ]
        )
        for element in sorted(elements)
    ]


#: proto3 has no scalar for a position, so the pair travels as a message with
#: named components -- never a `repeated double`, which is the transposition
#: the declaration exists to prevent.
_COORDINATE_MESSAGE = "\n".join(
    [
        "message Coordinate {",
        "  double lat = 1;",
        "  double lon = 2;",
        "}",
    ]
)


def _uses_coordinate(api: ApiModel) -> bool:
    def walk(ref: TypeRef) -> bool:
        return ref.kind == "coordinate" or any(walk(a) for a in ref.arguments)

    for model in api.models:
        if any(walk(field.type) for field in model.fields):
            return True
    for operation in api.operations:
        if walk(operation.response_body):
            return True
        if operation.request_body is not None and walk(operation.request_body):
            return True
        if any(walk(p.type) for p in operation.parameters):
            return True
    return False


def _package(title: str) -> str:
    """A proto package name from the API title: lowercase, `_`-joined."""
    cleaned = [c.lower() if c.isalnum() else " " for c in title]
    words = "".join(cleaned).split()
    return "_".join(words) or "api"


def _comment_text(value: str) -> str:
    return " ".join(value.splitlines())


def render_proto(api: ApiModel) -> dict[str, str]:
    """Return `{filename: contents}` for the proto target."""
    messages = []
    if _uses_coordinate(api):
        messages.append(_COORDINATE_MESSAGE)
    messages.extend(_message(model) for model in api.models)
    messages.extend(_page_messages(api))
    body = "\n\n".join(messages)
    header = (
        "// Generated by wreath typegen. Do not edit.\n"
        "//\n"
        f"// Message shapes for {_comment_text(api.title)} {_comment_text(api.version)}.\n"
        "//\n"
        "// FIELD NUMBERS COME FROM DECLARATION ORDER. Reordering a field in\n"
        "// the source dataclass renumbers it here, which is a wire-breaking\n"
        "// change that no test on either side will notice. Pin this file in\n"
        "// review, or declare the message with `wreath.protobuf`, where field\n"
        "// numbers are explicit and a reorder cannot move them.\n"
        "//\n"
        "// Messages only: `service` blocks are not generated. See the module\n"
        "// docstring in `wreath/typegen/targets/proto.py` for why.\n"
        "\n"
        'syntax = "proto3";\n'
        "\n"
        f"package {_package(api.title)};\n"
    )
    return {"api.proto": header + ("\n" + body + "\n" if body else "")}


__all__ = ["ProtoTargetError", "render_proto"]
