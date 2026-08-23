"""Typed handler binding and validation, compiled at startup.

A handler that takes only `request` runs exactly as before — binding costs
nothing unless a signature asks for more. Extra parameters are bound by
inspecting the signature once at route-compile time (never per request):

- a name matching a path placeholder becomes a **path parameter**, converted to
  the annotated scalar type,
- a parameter annotated with a **dataclass** or a mapped `wreath.orm` model
  receives the validated JSON body,
- every remaining scalar-annotated parameter comes from the **query string**,
  falling back to the handler default when absent.

The same compiled validator understands dataclasses, unions, literals, enums,
UUIDs, decimals, dates and instants, bytes, mappings, and list/set/tuple
containers. `Annotated[..., Field(...)]` supplies a wire alias, documentation
and numeric, length, or pattern constraints. A supported return annotation is
also a response contract: undeclared dataclass attributes are filtered and an
invalid result is refused before emission.

```python
@dataclass
class NewItem:
    name: str
    price: float
    tags: list[str] = field(default_factory=list)

@app.post("/items/{item_id}")
async def create(request, item_id: int, item: NewItem, dry_run: bool = False):
    ...
```

`Path`, `Query`, `Header`, `Cookie`, `Body`, `Form` and `File` override that
inference. **A marker rides in `Annotated`; the default stays a plain Python
default** — write `limit: Annotated[int, Query()] = 20`, never
`limit: int = Query(20)`. That second form is the most common mistake when
porting from FastAPI, and it used to fail quietly. Markers are read only from
`Annotated` metadata, so a marker written as a default is not recognised as a
marker at all: `Query(20)` would become the literal default value, every alias
and bound on it ignored, and a request that omits the parameter would hand the
marker object itself to the handler. It is therefore refused when routes
compile — a startup `TypeError` naming the parameter and the form to write
instead, rather than a wrong value at request time. `Depends` is the one
exception; it is written as the default, not in `Annotated`, and is not
refused.

Every binding failure raises `ValidationError`, and the application's error
boundary answers RFC 9457 `application/problem+json` — status 422, `detail`
"Request validation failed", and an `errors` member carrying one
`{"loc", "msg", "type"}` object per failure. There is no `{"detail": [...]}`
body; `loc` names the source first (`"path"`, `"query"`, `"header"`,
`"cookie"`, `"form"`, `"file"` or `"body"`) and then the field path.
`Wreath.set_validation_formatter` replaces that shaping. A request body that is
not JSON at all is a 400 instead, because nothing could be validated.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as _datetime
import enum
import inspect
import re
import sys as _sys
import types
import typing
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal, InvalidOperation
from time import monotonic_ns as _monotonic_ns
from typing import Any
from uuid import UUID

from ._awaitable import is_awaitable as _awaitable
from ._b64 import b64_encode as _b64encode_str
from ._codecs import parse_qs
from ._flight_markers import COV_PYTHON as _COV_PYTHON
from ._flight_markers import PH_DI_CONSTRUCT as _PH_DI_CONSTRUCT
from ._flight_markers import phase_marker as _phase_marker
from ._json import loads as _json_loads
from ._model_fields import dataclass_field_image
from ._native import _core
from .exceptions import BadRequest
from .geospatial import Coordinate
from .negotiation import PROTOBUF_MEDIA_TYPES as _PROTOBUF_MEDIA_TYPES
from .protobuf import ProtobufDecodeError as _ProtobufDecodeError
from .protobuf import decode as _protobuf_decode
from .protobuf import is_message as _is_message
from .request import Request
from .temporal import Instant, TemporalError

#: Mirrors `_routing.Handler`: a route handler may be `def` as well as
#: `async def`, so what it returns is awaited only when it is awaitable.
Handler = Callable[..., Awaitable[Any] | Any]

_MISSING = dataclasses.MISSING
_NONE_TYPE = type(None)
_BINDING_SPEC_UNSET = object()


@dataclasses.dataclass(frozen=True, slots=True)
class Field:
    """Describe and constrain one value inside `Annotated`.

    The same metadata drives request validation, response filtering, OpenAPI,
    MCP, and generated clients. `alias` changes the wire name of a dataclass
    field while the Python attribute keeps its declared name.
    """

    alias: str | None = None
    description: str | None = None
    examples: tuple[Any, ...] = ()
    gt: int | float | Decimal | None = None
    ge: int | float | Decimal | None = None
    lt: int | float | Decimal | None = None
    le: int | float | Decimal | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None

    def __post_init__(self) -> None:
        if self.alias == "":
            raise ValueError("Field alias must not be empty")
        if self.min_length is not None and self.min_length < 0:
            raise ValueError("Field min_length must be non-negative")
        if self.max_length is not None and self.max_length < 0:
            raise ValueError("Field max_length must be non-negative")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("Field min_length exceeds max_length")
        if self.pattern is not None:
            re.compile(self.pattern)


def _field_annotation(annotation: Any) -> tuple[Any, Field | None]:
    if typing.get_origin(annotation) is not typing.Annotated:
        return annotation, None
    base, *metadata = typing.get_args(annotation)
    fields = [item for item in metadata if isinstance(item, Field)]
    if len(fields) > 1:
        raise TypeError("an annotation may carry at most one Field")
    return base, fields[0] if fields else None


# Body-validation plan opcodes. These mirror the enum in
# src/wreath/_native/validate.c; the native validator executes a plan compiled
# here once per body type, and the Python `validate` remains the reference.
_OP_ANY = 0
_OP_NULL = 1
_OP_INT = 2
_OP_FLOAT = 3
_OP_BOOL = 4
_OP_STR = 5
_OP_LIST = 6
_OP_DICT = 7
_OP_UNION = 8
_OP_DATACLASS = 9
_OP_UNSUPPORTED = 10
_OP_FIELD = 11


class _PlanUnsupported(Exception):
    """A body plan could not be compiled (e.g. a recursive dataclass); the
    caller falls back to the Python validator, which recurses lazily."""


def _compile_plan(annotation: Any, seen: frozenset[type]) -> tuple[Any, ...]:
    """Compile `annotation` into the native validator's plan tuples.

    Raises :class:`_PlanUnsupported` for shapes the flat plan cannot express
    (currently recursive dataclasses), signalling recursive evaluation.
    Semantics mirror :func:`_validate` exactly.
    """
    annotation, field = _field_annotation(annotation)
    if field is not None:
        child = _compile_plan(annotation, seen)
        comparisons = tuple(
            (
                opcode,
                bound,
                f"value must be {symbol} {bound}",
                f"{kind} requires a comparable value",
                kind,
            )
            for opcode, bound, symbol, kind in (
                (0, field.gt, ">", "gt"),
                (1, field.ge, ">=", "ge"),
                (2, field.lt, "<", "lt"),
                (3, field.le, "<=", "le"),
            )
            if bound is not None
        )
        lengths = tuple(
            (
                opcode,
                bound,
                f"length must be {symbol} {bound}",
                f"{kind} requires a sized value",
                kind,
            )
            for opcode, bound, symbol, kind in (
                (0, field.min_length, ">=", "min_length"),
                (1, field.max_length, "<=", "max_length"),
            )
            if bound is not None
        )
        pattern = (
            None
            if field.pattern is None
            else (
                re.compile(field.pattern).search,
                f"value does not match {field.pattern!r}",
                "pattern",
            )
        )
        # Alias, description and examples affect the wire schema but do not
        # constrain a value. Avoid wrapping the child plan when there is no
        # request-time work to perform.
        if not comparisons and not lengths and pattern is None:
            return child
        return (_OP_FIELD, child, comparisons, lengths, pattern)
    if annotation in (Any, object) or annotation is inspect.Parameter.empty:
        return (_OP_ANY,)
    if annotation is None or annotation is _NONE_TYPE:
        return (_OP_NULL,)
    origin = typing.get_origin(annotation)
    if origin is None:
        if annotation is float:
            return (_OP_FLOAT,)
        if annotation is int:
            return (_OP_INT,)
        if annotation is bool:
            return (_OP_BOOL,)
        if annotation is str:
            return (_OP_STR,)
        if dataclasses.is_dataclass(annotation):
            if annotation in seen:
                raise _PlanUnsupported
            child_seen = seen | {annotation}
            fields_list: list[tuple[str, str, tuple[Any, ...], int]] = []
            for name, wire_name, field_annotation, required in _dataclass_wire_spec(annotation):
                fields_list.append(
                    (
                        name,
                        wire_name,
                        _compile_plan(field_annotation, child_seen),
                        1 if required else 0,
                    )
                )
            fields = tuple(fields_list)
            return (_OP_DATACLASS, annotation, fields)
        if (
            annotation
            in (
                Decimal,
                UUID,
                bytes,
                _datetime.date,
                _datetime.datetime,
                Instant,
                Coordinate,
            )
            or isinstance(annotation, type)
            and issubclass(annotation, enum.Enum)
        ):
            raise _PlanUnsupported
        return (_OP_UNSUPPORTED, f"unsupported annotation {annotation!r}")
    if origin is typing.Literal:
        raise _PlanUnsupported
    if origin in (types.UnionType, typing.Union):
        options = typing.get_args(annotation)
        has_none = 1 if _NONE_TYPE in options else 0
        compiled = tuple(
            _compile_plan(option, seen) for option in options if option is not _NONE_TYPE
        )
        return (_OP_UNION, has_none, compiled, f"value matches no option of {annotation}")
    if origin is list:
        args = typing.get_args(annotation)
        return (_OP_LIST, _compile_plan(args[0] if args else Any, seen))
    if origin in (tuple, set, frozenset):
        raise _PlanUnsupported
    if origin is dict:
        args = typing.get_args(annotation)
        return (_OP_DICT, _compile_plan(args[1] if len(args) == 2 else Any, seen))
    return (_OP_UNSUPPORTED, f"unsupported annotation {annotation!r}")


class ValidationError(Exception):
    """The field errors collected from one request, converted to a 422.

    Raised by `validate`, by path/query/header/cookie/form scalar conversion, by
    a numeric `Query` range, and by the compiled ORM model validators. The
    application's error boundary catches it and answers RFC 9457
    `application/problem+json` with status 422, detail "Request validation
    failed", and an `errors` member holding this list verbatim.
    `Wreath.set_validation_formatter` reshapes that body;
    `Wreath.add_exception_handler(ValidationError, ...)` replaces the response
    outright.

    The collected list is available as the `errors` attribute. `str(error)` is
    only a count summary — everything a client needs is in `errors`.

    Args:
        errors: One dict per failure, with keys `loc`, `msg` and `type`.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        """Keep `errors` and set the exception message to a count summary."""
        super().__init__(f"{len(errors)} validation error(s)")
        self.errors = errors


class ResponseValidationError(Exception):
    """A handler returned a value that violates its declared public contract.

    This is deliberately distinct from `ValidationError`: bad request data is a
    caller error and answers 422, while a bad response is an application defect
    and passes through the ordinary 500 boundary.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} response validation error(s)")


def _error(loc: tuple[Any, ...], message: str, kind: str) -> dict[str, Any]:
    return {"loc": list(loc), "msg": message, "type": kind}


# --- JSON value validation ------------------------------------------------------


# Total node visits allowed per validation. A union tries every option against
# the whole value, so nested unions that fail deep re-explore each branch at
# every level -- O(2**depth) work from a small body (a validation bomb). This
# ceiling bounds the worst case: the densest legitimate body under the default
# max_body_bytes (1 MiB) decodes to ~500k nodes, so 2M leaves ~4x headroom and
# is never reached by real input, while a bomb stops with one "too_complex"
# error instead of hanging. Kept in lockstep with WREATH_VALIDATE_MAX_STEPS in
# the native validate.c.
_VALIDATE_MAX_STEPS = 2_000_000


def validate(annotation: Any, value: Any, loc: tuple[Any, ...] = ()) -> Any:
    """Validate a decoded-JSON `value` against `annotation`.

    Body validation for the shapes the flat plan cannot express -- recursive
    dataclasses, mostly. Everything else compiles to a plan and is checked in C
    by `_core.run_validation`, with identical semantics and identical error
    ordering.

    Understood annotations are the scalars `str`, `int`, `float` and `bool`,
    `Any`, `None`, `list[T]`, `tuple[T, ...]` (from list input), `dict[str, T]`,
    unions including `Optional`, and dataclasses, recursively. Anything else
    is reported as an `unsupported` error rather than passed through. A JSON
    number binds to `float` whether or not it was written with a decimal point,
    but `bool` never satisfies `int` or `float`. A dataclass rejects a field it
    does not declare and reports each missing required field. Every error in the
    whole value is collected before raising, not just the first.

    Validation is bounded to two million node visits, because a nest of unions
    re-explores each branch at every level and would otherwise let a small body
    cost exponential work. Exceeding it aborts with one `too_complex` error at
    the root and constructs nothing from the truncated result.

    Args:
        annotation: The type to check against, as it appears on the parameter.
        value: A decoded JSON value — dicts, lists, strings, numbers, bools, None.
        loc: Path prefix for reported errors, such as `("body",)`.

    Returns:
        The validated value, with dataclasses constructed and ints widened to floats.

    Raises:
        ValidationError: The value did not match, or the step budget ran out.
    """
    errors: list[dict[str, Any]] = []
    budget = [_VALIDATE_MAX_STEPS]
    result = _validate(annotation, value, loc, errors, budget)
    if budget[0] < 0:
        # Cut short by the step budget: report once, at the root, regardless of
        # which subtree exhausted it (mirrors native wreath_run_validation).
        errors.append(_error(loc, "value is too complex to validate", "too_complex"))
    if errors:
        raise ValidationError(errors)
    return result


def _validate(annotation: Any, value: Any, loc: tuple[Any, ...], errors: list, budget: list) -> Any:
    annotation, field = _field_annotation(annotation)
    if field is not None:
        before = len(errors)
        result = _validate(annotation, value, loc, errors, budget)
        if len(errors) == before and budget[0] >= 0:
            _validate_field(field, result, loc, errors)
        return result
    if budget[0] <= 0:
        # Budget exhausted: mark it (negative sentinel) and stop descending.
        budget[0] = -1
        return value
    budget[0] -= 1
    if annotation in (Any, object) or annotation is inspect.Parameter.empty:
        return value
    if annotation is None or annotation is _NONE_TYPE:
        if value is not None:
            errors.append(_error(loc, "value must be null", "null"))
        return None

    origin = typing.get_origin(annotation)
    if origin is None:
        if annotation is float:
            # JSON has one number type: accept ints as floats.
            if isinstance(value, float):
                return value
            if isinstance(value, int) and not isinstance(value, bool):
                return float(value)
            errors.append(_error(loc, "value is not a number", "float"))
            return value
        if annotation is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            errors.append(_error(loc, "value is not an integer", "int"))
            return value
        if annotation is bool:
            if isinstance(value, bool):
                return value
            errors.append(_error(loc, "value is not a boolean", "bool"))
            return value
        if annotation is str:
            if isinstance(value, str):
                return value
            errors.append(_error(loc, "value is not a string", "str"))
            return value
        if annotation is Decimal:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                errors.append(_error(loc, "value is not a decimal", "decimal"))
                return value
            try:
                return Decimal(str(value))
            except InvalidOperation:
                errors.append(_error(loc, "value is not a decimal", "decimal"))
                return value
        if annotation is UUID:
            if not isinstance(value, str):
                errors.append(_error(loc, "value is not a UUID", "uuid"))
                return value
            try:
                return UUID(value)
            except ValueError:
                errors.append(_error(loc, "value is not a UUID", "uuid"))
                return value
        if annotation is bytes:
            if not isinstance(value, str):
                errors.append(_error(loc, "value is not base64 text", "bytes"))
                return value
            try:
                return base64.b64decode(value, validate=True)
            except ValueError:
                errors.append(_error(loc, "value is not base64 text", "bytes"))
                return value
        if annotation is Instant or annotation is _datetime.datetime:
            if not isinstance(value, str):
                errors.append(_error(loc, "value is not an ISO-8601 instant", "instant"))
                return value
            try:
                return Instant.parse(value)
            except TemporalError as error:
                errors.append(_error(loc, str(error), "instant"))
                return value
        if annotation is Coordinate:
            # An object, never a bare pair. GeoJSON orders `[lon, lat]` and
            # people say "lat, lon", so a two-element array is ambiguous at
            # exactly the moment it matters -- and `Coordinate(...)` refuses
            # positional arguments for that reason. Accepting one here would
            # reopen the trap at the wire.
            if not isinstance(value, dict):
                errors.append(_error(loc, "value is not a {lat, lon} object", "coordinate"))
                return value
            if set(value) != {"lat", "lon"}:
                errors.append(_error(loc, "value needs exactly lat and lon", "coordinate"))
                return value
            try:
                return Coordinate(lat=value["lat"], lon=value["lon"])
            except (TypeError, ValueError) as error:
                errors.append(_error(loc, str(error), "coordinate"))
                return value
        if annotation is _datetime.date:
            if not isinstance(value, str):
                errors.append(_error(loc, "value is not an ISO-8601 date", "date"))
                return value
            try:
                return _datetime.date.fromisoformat(value)
            except ValueError:
                errors.append(_error(loc, "value is not an ISO-8601 date", "date"))
                return value
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            try:
                return annotation(value)
            except TypeError, ValueError:
                errors.append(_error(loc, "value is not an allowed enum member", "enum"))
                return value
        if dataclasses.is_dataclass(annotation):
            return _validate_dataclass(annotation, value, loc, errors, budget)
        errors.append(_error(loc, f"unsupported annotation {annotation!r}", "unsupported"))
        return value

    if origin is typing.Literal:
        choices = typing.get_args(annotation)
        if any(type(value) is type(choice) and value == choice for choice in choices):
            return value
        errors.append(_error(loc, "value is not one of the allowed literals", "literal"))
        return value

    if origin in (types.UnionType, typing.Union):
        options = typing.get_args(annotation)
        if value is None and _NONE_TYPE in options:
            return None
        for option in options:
            if option is _NONE_TYPE:
                continue
            attempt: list[dict[str, Any]] = []
            result = _validate(option, value, loc, attempt, budget)
            if not attempt and budget[0] >= 0:
                return result
            if budget[0] < 0:
                # Budget ran out mid-union: an empty attempt from here is a
                # silent bail, not a real match, so stop trying options. The
                # top-level reports too_complex.
                return value
        errors.append(_error(loc, f"value matches no option of {annotation}", "union"))
        return value

    if origin in (list, tuple, set, frozenset):
        if not isinstance(value, list):
            errors.append(_error(loc, "value is not an array", "list"))
            return value
        args = typing.get_args(annotation)
        if origin is tuple and args and not (len(args) == 2 and args[1] is Ellipsis):
            if len(value) != len(args):
                errors.append(_error(loc, f"array must contain exactly {len(args)} items", "tuple"))
                return value
            result = tuple(
                _validate(item_type, item, (*loc, index), errors, budget)
                for index, (item_type, item) in enumerate(zip(args, value, strict=True))
            )
            return result
        item_type = args[0] if args else Any
        items = [
            _validate(item_type, item, (*loc, index), errors, budget)
            for index, item in enumerate(value)
        ]
        if origin is tuple:
            return tuple(items)
        if origin is set:
            try:
                return set(items)
            except TypeError:
                errors.append(_error(loc, "array items are not hashable", "set"))
                return value
        if origin is frozenset:
            try:
                return frozenset(items)
            except TypeError:
                errors.append(_error(loc, "array items are not hashable", "set"))
                return value
        return items

    if origin is dict:
        if not isinstance(value, dict):
            errors.append(_error(loc, "value is not an object", "dict"))
            return value
        args = typing.get_args(annotation)
        value_type = args[1] if len(args) == 2 else Any
        return {
            key: _validate(value_type, item, (*loc, key), errors, budget)
            for key, item in value.items()
        }

    errors.append(_error(loc, f"unsupported annotation {annotation!r}", "unsupported"))
    return value


def _validate_field(
    field: Field,
    value: Any,
    loc: tuple[Any, ...],
    errors: list[dict[str, Any]],
) -> None:
    for bound, comparison, message, kind in (
        (field.gt, lambda item, limit: item > limit, "value must be >", "gt"),
        (field.ge, lambda item, limit: item >= limit, "value must be >=", "ge"),
        (field.lt, lambda item, limit: item < limit, "value must be <", "lt"),
        (field.le, lambda item, limit: item <= limit, "value must be <=", "le"),
    ):
        if bound is None:
            continue
        try:
            valid = comparison(value, bound)
        except TypeError:
            errors.append(_error(loc, f"{kind} requires a comparable value", kind))
            return
        if not valid:
            errors.append(_error(loc, f"{message} {bound}", kind))
            return
    if field.min_length is not None:
        try:
            valid = len(value) >= field.min_length
        except TypeError:
            errors.append(_error(loc, "min_length requires a sized value", "min_length"))
            return
        if not valid:
            errors.append(_error(loc, f"length must be >= {field.min_length}", "min_length"))
            return
    if field.max_length is not None:
        try:
            valid = len(value) <= field.max_length
        except TypeError:
            errors.append(_error(loc, "max_length requires a sized value", "max_length"))
            return
        if not valid:
            errors.append(_error(loc, f"length must be <= {field.max_length}", "max_length"))
            return
    if field.pattern is not None:
        if not isinstance(value, str) or re.search(field.pattern, value) is None:
            errors.append(_error(loc, f"value does not match {field.pattern!r}", "pattern"))


def _response_input(annotation: Any, value: Any) -> Any:
    """Project a raw handler value onto its declared response shape."""
    annotation, _field = _field_annotation(annotation)
    if dataclasses.is_dataclass(annotation):
        if isinstance(value, annotation):
            return value
        if not isinstance(value, Mapping):
            return value
        return {
            wire_name: _response_input(field_annotation, value[wire_name])
            for _name, wire_name, field_annotation, _required in _dataclass_wire_spec(annotation)
            if wire_name in value
        }
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (list, set, frozenset) and isinstance(value, (list, tuple, set, frozenset)):
        item_type = args[0] if args else Any
        return [_response_input(item_type, item) for item in value]
    if origin is tuple and isinstance(value, (list, tuple)):
        if len(args) == 2 and args[1] is Ellipsis:
            return [_response_input(args[0], item) for item in value]
        return [
            _response_input(item_type, item) for item_type, item in zip(args, value, strict=False)
        ]
    if origin is dict and isinstance(value, Mapping):
        item_type = args[1] if len(args) == 2 else Any
        return {key: _response_input(item_type, item) for key, item in value.items()}
    return value


def _jsonable_hook(value: Any, convert: Callable[[Any], Any]) -> Any:
    """Apply the opt-in JSON protocol, then resume the canonical conversion."""
    hook = getattr(type(value), "__jsonable__", None)
    if not callable(hook):
        return value
    rendered = hook(value)
    if rendered is value:
        raise TypeError(f"object of type {type(value).__name__} returned itself from __jsonable__")
    return convert(rendered)


def _response_union_match(annotation: Any, value: Any) -> bool:
    """Whether a validated response value belongs to this union arm.

    Validation has already resolved mapping-shaped dataclass inputs into their
    declared class, so the hot case is one exact runtime type check per arm.
    ``Any`` remains the final catch-all and is intentionally not allowed to
    claim a value before a concrete arm.
    """
    annotation, _field = _field_annotation(annotation)
    if annotation is Any or annotation is object:
        return True
    if annotation is _NONE_TYPE:
        return value is None
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return value in typing.get_args(annotation)
    target = origin or annotation
    return isinstance(target, type) and isinstance(value, target)


def _jsonable(annotation: Any, value: Any) -> Any:
    annotation, _field = _field_annotation(annotation)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (types.UnionType, typing.Union):
        for option in args:
            if _response_union_match(option, value):
                return _jsonable(option, value)
    if dataclasses.is_dataclass(annotation) and isinstance(value, annotation):
        return {
            wire_name: _jsonable(field_annotation, getattr(value, name))
            for name, wire_name, field_annotation, _required in _dataclass_wire_spec(annotation)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return _b64encode_str(value)
    if isinstance(value, (bytearray, memoryview)):
        return _b64encode_str(bytes(value))
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        item_type = args[0] if args else Any
        return [_jsonable(item_type, item) for item in value]
    if isinstance(value, Mapping):
        item_type = args[1] if origin is dict and len(args) == 2 else Any
        return {key: _jsonable(item_type, item) for key, item in value.items()}
    return _jsonable_hook(value, _jsonable_any)


#: Error path every response check reports under, built once rather than per
#: response.
_RESPONSE_LOC = ("response",)


def _identity(value: Any) -> Any:
    return value


#: Types that are already JSON primitives and cannot be anything else.
#:
#: Tested with `type(value) in`, never `isinstance`, and that is the whole
#: point: `class Color(str, Enum)` *is* a `str`, and `_jsonable` must still
#: reduce it to `value.value`. An exact-type test lets the plain scalars --
#: which is nearly every leaf of nearly every response -- leave before the
#: six-branch `isinstance` ladder, while every subclass falls into it.
_JSON_SCALARS = frozenset({str, int, float, bool, type(None)})


def _jsonable_any(value: Any) -> Any:
    """`_jsonable(Any, value)`, with the annotation walk removed.

    `Any` is where every compiled walk bottoms out -- an unparameterized
    container's item type, a union arm's fallback, a dataclass field declared
    `Any`. Compiling it like the rest would recurse forever (its item type is
    `Any` again), so it is written once, closed over itself.
    """
    kind = type(value)
    if kind in _JSON_SCALARS:
        return value
    # A plain `dict` or `list` before the scalar ladder, by *exact* type.
    #
    # The ladder below is ordered by how specific each case is, which put the
    # commonest shape a handler returns -- a dict -- behind seven guards that
    # cannot match it, ending in `isinstance(value, Mapping)`. That last one is
    # an ABC check and the most expensive of the eight: measured on this
    # machine, the eight guards cost 578ns of a 790ns walk over `{"id": 42,
    # "ok": True}`, and `type(value) is dict` answers the same question in 28ns.
    #
    # `is dict` rather than `isinstance`, so this cannot change what any
    # *subclass* does: anything that is not exactly a dict or a list falls
    # through to the original ladder in its original order. A dict subclass
    # still reaches `Mapping`, and one that is also an `Enum` still resolves as
    # an `Enum`, exactly as before.
    if kind is dict:
        return {
            key: (item if type(item) in _JSON_SCALARS else _jsonable_any(item))
            for key, item in value.items()
        }
    if kind is list:
        return [item if type(item) in _JSON_SCALARS else _jsonable_any(item) for item in value]
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return _b64encode_str(value)
    if isinstance(value, (bytearray, memoryview)):
        return _b64encode_str(bytes(value))
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item if type(item) in _JSON_SCALARS else _jsonable_any(item) for item in value]
    if isinstance(value, Mapping):
        return {
            key: (item if type(item) in _JSON_SCALARS else _jsonable_any(item))
            for key, item in value.items()
        }
    return _jsonable_hook(value, _jsonable_any)


def _compile_response_input(
    annotation: Any, seen: frozenset[Any] = frozenset()
) -> Callable[[Any], Any]:
    """Compile `_response_input`'s annotation walk once, at route-compile time.

    `_response_input` reads the annotation and the value together, so it pays
    `typing.get_origin`, `typing.get_args` and `_field_annotation` at every node
    of the annotation on every single response. None of that can differ between
    responses: the annotation is fixed when the route compiles.

    So the walk happens here instead, and what comes back is a closure that
    descends only the *value*. `_response_input` stays as the definition the
    compiled form is crossed against -- see
    `tests/test_binding_response_compilation.py`.
    """
    annotation, _field = _field_annotation(annotation)
    if dataclasses.is_dataclass(annotation):
        if annotation in seen:
            # Self-referential: the walk cannot be finite. Interpret this node
            # and let the compiled parents keep their gain.
            return lambda value, _a=annotation: _response_input(_a, value)
        child_seen = seen | {annotation}
        fields = tuple(
            (wire_name, _compile_response_input(field_annotation, child_seen))
            for _name, wire_name, field_annotation, _required in _dataclass_wire_spec(annotation)
        )

        def project_dataclass(value: Any, _cls: Any = annotation, _fields: Any = fields) -> Any:
            if isinstance(value, _cls) or not isinstance(value, Mapping):
                return value
            return {
                wire_name: project(value[wire_name])
                for wire_name, project in _fields
                if wire_name in value
            }

        return project_dataclass

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (list, set, frozenset):
        item = _compile_response_input(args[0] if args else Any, seen)

        def project_sequence(value: Any, _item: Any = item) -> Any:
            if isinstance(value, (list, tuple, set, frozenset)):
                return [_item(entry) for entry in value]
            return value

        return project_sequence
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            item = _compile_response_input(args[0], seen)

            def project_variadic_tuple(value: Any, _item: Any = item) -> Any:
                if isinstance(value, (list, tuple)):
                    return [_item(entry) for entry in value]
                return value

            return project_variadic_tuple
        items = tuple(_compile_response_input(arg, seen) for arg in args)

        def project_tuple(value: Any, _items: Any = items) -> Any:
            if isinstance(value, (list, tuple)):
                return [item(entry) for item, entry in zip(_items, value, strict=False)]
            return value

        return project_tuple
    if origin is dict:
        item = _compile_response_input(args[1] if len(args) == 2 else Any, seen)

        def project_mapping(value: Any, _item: Any = item) -> Any:
            if isinstance(value, Mapping):
                return {key: _item(entry) for key, entry in value.items()}
            return value

        return project_mapping
    return _identity


def _projection_is_identity(annotation: Any, seen: frozenset[Any] = frozenset()) -> bool:
    """Whether `_response_input` provably returns its input for every value.

    The projection does exactly two things: it filters a mapping down to a
    dataclass's declared wire names, and it rebuilds containers. So it can only
    change a value when the annotation tree holds a dataclass -- something to
    filter -- or a sequence origin, which turns a `tuple` or `set` input into a
    `list` on the way out.

    `dict[str, Any]` is neither, and it is the shape an idiomatic JSON handler
    returns. Recognizing it here removes a walk that allocated a new mapping
    with the same contents and handed it straight to the validator.

    Deliberately conservative: `list[int]` is *usually* identity too, but only
    when the handler already returned a list, and a compiler cannot know that.
    Answering `True` there would change what the validator is handed.
    """
    annotation, _field = _field_annotation(annotation)
    if annotation in seen:
        return False
    if dataclasses.is_dataclass(annotation):
        return False
    origin = typing.get_origin(annotation)
    if origin in (list, set, frozenset, tuple):
        return False
    args = typing.get_args(annotation)
    if origin is dict:
        return len(args) != 2 or _projection_is_identity(args[1], seen | {annotation})
    if args:
        child = seen | {annotation}
        return all(_projection_is_identity(arg, child) for arg in args)
    return True


def _response_plan_is_wire_preserving(plan: tuple[Any, ...]) -> bool:
    """Whether successful validation leaves every JSON node unchanged.

    Such a plan can run straight from the handler's Python result to the
    native JSON writer.  The validation kernel may inspect the input graph but
    never constructs a replacement graph for these opcodes; its only Python
    outputs are the final bytes or final validation errors.  Float widening
    and dataclass construction are deliberately excluded because both change
    values before they reach the wire.
    """
    opcode = plan[0]
    if opcode in (_OP_ANY, _OP_NULL, _OP_INT, _OP_BOOL, _OP_STR):
        return True
    if opcode in (_OP_LIST, _OP_DICT, _OP_FIELD):
        return _response_plan_is_wire_preserving(plan[1])
    if opcode == _OP_UNION:
        return all(map(_response_plan_is_wire_preserving, plan[2]))
    return False


def _compile_jsonable(annotation: Any, seen: frozenset[Any] = frozenset()) -> Callable[[Any], Any]:
    """Compile `_jsonable`'s annotation walk once, at route-compile time.

    The same hoist as `_compile_response_input`, and the same contract: the
    returned closure dispatches on the *value's* runtime type exactly as
    `_jsonable` does -- a `UUID` inside a `list[Any]` still stringifies -- and
    consults the annotation only for what the compiler already resolved.
    """
    annotation, _field = _field_annotation(annotation)
    if annotation is Any or annotation is object or annotation is inspect.Parameter.empty:
        return _jsonable_any
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (types.UnionType, typing.Union):
        exact: dict[type, Callable[[Any], Any]] = {}
        subclasses: list[tuple[type, Callable[[Any], Any]]] = []
        literals: list[tuple[tuple[Any, ...], Callable[[Any], Any]]] = []
        fallback = _jsonable_any
        for raw_option in args:
            option, _option_field = _field_annotation(raw_option)
            convert = _compile_jsonable(option, seen | {annotation})
            if option is Any or option is object:
                fallback = convert
                continue
            option_origin = typing.get_origin(option)
            if option_origin is typing.Literal:
                literals.append((typing.get_args(option), convert))
                continue
            target = option_origin or option
            if isinstance(target, type):
                exact[target] = convert
                subclasses.append((target, convert))

        def jsonable_union(
            value: Any,
            _exact: Any = exact,
            _subclasses: Any = tuple(subclasses),
            _literals: Any = tuple(literals),
            _fallback: Any = fallback,
        ) -> Any:
            convert = _exact.get(type(value))
            if convert is not None:
                return convert(value)
            for target, convert in _subclasses:
                if isinstance(value, target):
                    return convert(value)
            for choices, convert in _literals:
                if value in choices:
                    return convert(value)
            return _fallback(value)

        return jsonable_union
    if dataclasses.is_dataclass(annotation):
        if annotation in seen:
            return lambda value, _a=annotation: _jsonable(_a, value)
        child_seen = seen | {annotation}
        fields = tuple(
            (name, wire_name, _compile_jsonable(field_annotation, child_seen))
            for name, wire_name, field_annotation, _required in _dataclass_wire_spec(annotation)
        )

        # A handler annotated with a dataclass may still return a mapping or a
        # list; `_jsonable` falls through to its value dispatch in that case,
        # with `Any` item types, and so does this.
        def jsonable_dataclass(value: Any, _cls: Any = annotation, _fields: Any = fields) -> Any:
            if isinstance(value, _cls):
                # The scalar test is inlined rather than left to `to_json`,
                # which would answer it identically one function call later.
                # Nearly every field of nearly every model is a plain scalar,
                # so that call was the largest remaining cost of serializing
                # one -- see the walrus, which binds the attribute exactly once.
                return {
                    wire_name: (
                        field
                        if type(field := getattr(value, name)) in _JSON_SCALARS
                        else to_json(field)
                    )
                    for name, wire_name, to_json in _fields
                }
            return _jsonable_any(value)

        return jsonable_dataclass

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    sequence_item = _compile_jsonable(args[0], seen) if args else _jsonable_any
    mapping_item = (
        _compile_jsonable(args[1], seen) if origin is dict and len(args) == 2 else _jsonable_any
    )

    def jsonable_value(
        value: Any, _sequence: Any = sequence_item, _mapping: Any = mapping_item
    ) -> Any:
        kind = type(value)
        if kind in _JSON_SCALARS:
            return value
        # The exact-type fast paths, for the reason given on `_jsonable_any`:
        # the ladder below cannot match a plain dict until its eighth and most
        # expensive guard. Both arms keep the annotation's own converters, so
        # this changes when the ladder is entered and never what it decides.
        if kind is dict:
            return {
                key: (entry if type(entry) in _JSON_SCALARS else _mapping(entry))
                for key, entry in value.items()
            }
        if kind is list:
            return [entry if type(entry) in _JSON_SCALARS else _sequence(entry) for entry in value]
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, bytes):
            return _b64encode_str(value)
        if isinstance(value, (bytearray, memoryview)):
            return _b64encode_str(bytes(value))
        if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
            return value.isoformat()
        # Same inlining as the dataclass arm: a scalar element answers here
        # rather than one call down, and elements are overwhelmingly scalars.
        if isinstance(value, (list, tuple, set, frozenset)):
            return [entry if type(entry) in _JSON_SCALARS else _sequence(entry) for entry in value]
        if isinstance(value, Mapping):
            return {
                key: (entry if type(entry) in _JSON_SCALARS else _mapping(entry))
                for key, entry in value.items()
            }
        return _jsonable_hook(value, _jsonable_any)

    return jsonable_value


def _compile_response_check(annotation: Any) -> Callable[[Any], Any]:
    """Compile the response's validation step where the plan allows.

    The request body has executed a flat native plan since `_body_validator`
    was written; the response side kept walking the annotation in Python for
    every value it checked, which is the same work at eleven times the price.
    This closes that gap -- same plan compiler, same validator, same error
    ordering.
    """
    try:
        plan = _compile_plan(annotation, frozenset())
    except _PlanUnsupported:
        plan = None
    if plan is not None:
        run_validation = _core.run_validation

        def planned_check(value: Any, _plan: Any = plan, _run: Any = run_validation) -> Any:
            result, errors = _run(_plan, value, _RESPONSE_LOC)
            if errors:
                raise ValidationError(errors)
            return result

        return planned_check

    def annotation_check(value: Any, _annotation: Any = annotation) -> Any:
        return validate(_annotation, value, _RESPONSE_LOC)

    return annotation_check


def compile_response_validator(handler: Handler, annotation: Any) -> Handler:
    """Filter and validate a handler's annotated return value.

    Response objects retain full control of their wire representation. Plain
    values are projected onto the annotation (dropping undeclared dataclass
    fields), validated, and converted to dependency-free JSON primitives.
    """
    if annotation is Any or annotation is inspect.Parameter.empty:
        return handler
    if annotation in (dict, list, tuple, set, frozenset):
        # An unsubscripted container says nothing about its contents, so it is
        # documentation rather than an enforceable output contract. Existing
        # handlers commonly use ``-> dict``; wrapping those as though they had
        # declared a complete schema turned every successful response into a
        # response-validation 500.
        return handler
    from .response import (
        FileResponse,
        PreparedResponse,
        Response,
        StreamingResponse,
        _EncodedJSON,
    )

    response_types = (Response, StreamingResponse, FileResponse, PreparedResponse)
    if isinstance(annotation, type) and issubclass(annotation, response_types):
        return handler

    # Three walks of the same fixed annotation, hoisted out of the request --
    # and the first of them dropped entirely where it provably does nothing.
    projection_is_identity = _projection_is_identity(annotation)
    check = _compile_response_check(annotation)
    if not projection_is_identity:
        project = _compile_response_input(annotation)
        validate_only = check

        def check(value: Any, _project: Any = project, _check: Any = validate_only) -> Any:
            return _check(_project(value))

    to_json = _compile_jsonable(annotation)
    planned_json: Callable[[Any], tuple[bytes | None, list[dict[str, Any]]]] | None = None
    try:
        response_plan = _compile_plan(annotation, frozenset())
    except _PlanUnsupported:
        pass
    else:
        if response_plan[0] in (_OP_LIST, _OP_DICT) and _response_plan_is_wire_preserving(
            response_plan
        ):
            run_validation_json = _core.run_validation_json

            def planned_json(  # type: ignore[no-redef]
                value: Any,
                _plan: Any = response_plan,
                _run: Any = run_validation_json,
            ) -> tuple[bytes | None, list[dict[str, Any]]]:
                return _run(_plan, value, _RESPONSE_LOC)

    def _validated(value: Any) -> Any:
        if isinstance(value, response_types):
            return value
        if planned_json is not None:
            try:
                body, errors = planned_json(value)
            except TypeError:
                # The validation plan may accept a value the compact encoder
                # deliberately does not (UUID, Decimal, bytes, sets).  Those
                # retain the full conversion definition below.  Ordinary JSON
                # values never raise and therefore never pay that Python walk.
                pass
            else:
                if errors:
                    # A sequence projection may make a tuple/set acceptable,
                    # including below a dict.  Only a plan whose projection is
                    # provably identity may turn the first refusal into the
                    # public response error; otherwise the canonical path gets
                    # its chance to project and validate the value.
                    if projection_is_identity:
                        error = ValidationError(errors)
                        raise ResponseValidationError(errors) from error
                else:
                    if body is None:
                        raise RuntimeError("native response encoder returned no body")
                    return _EncodedJSON(body)
        try:
            validated = check(value)
        except ValidationError as error:
            raise ResponseValidationError(error.errors) from error
        return to_json(validated)

    checked: Handler
    if inspect.iscoroutinefunction(handler):

        async def checked(request: Request) -> Any:  # type: ignore[no-redef]
            return _validated(await handler(request))

    else:
        # A `def` handler stays `def` through this wrapper, so a synchronous
        # route keeps its synchronous call convention all the way to dispatch
        # rather than acquiring a coroutine here.
        def checked(request: Request) -> Any:  # type: ignore[no-redef]
            return _validated(handler(request))

    checked.__name__ = getattr(handler, "__name__", "checked")
    checked.__qualname__ = getattr(handler, "__qualname__", "checked")
    return checked


def compile_message_negotiation(handler: Handler, annotation: Any) -> Handler:
    """Offer protobuf on a route whose return annotation is a `@message`.

    Wreath has read protobuf request bodies since `_decode_protobuf_body`
    landed, and could not write one: a handler returning a declared message
    served JSON whatever the client asked for. That asymmetry was not a
    decision, it was the half that had not been done.

    `wreath.negotiation` keeps `PROTOBUF` out of `DEFAULT_SERIALIZERS` for a
    reason that stands: JSON and MessagePack encode whatever a handler returns,
    protobuf can only encode a *declared* message, and a handler returning a
    dict is the common case — so a global offer would turn every existing route
    into a runtime error for any client sending `Accept: application/x-protobuf`.

    **The return annotation is the fact that was missing.** A route annotated
    `-> Ping` has said at startup that its body is encodable, so the offer is
    made for that route and nowhere else. A route without one is returned
    unchanged and pays nothing at all — not even a branch.

    This is compiled *before* `compile_response_validator`, because that
    validator projects a value onto JSON primitives and the encoder needs the
    message itself. A `Response` already passes through the validator untouched,
    which is how the negotiated result reaches the wire without a second path.
    """
    if not (isinstance(annotation, type) and _is_message(annotation)):
        return handler
    from .negotiation import JSON, MSGPACK, PROTOBUF, negotiate
    from .response import FileResponse, PreparedResponse, Response, StreamingResponse

    passthrough = (Response, StreamingResponse, FileResponse, PreparedResponse)
    # JSON first, so a missing `Accept` and `*/*` both still resolve to it --
    # adding an offer must not change what an existing client already gets.
    offers = (JSON, MSGPACK, PROTOBUF)
    media = PROTOBUF.media_type.encode("ascii")

    def _negotiated(request: Request, value: Any) -> Any:
        # Only an explicit protobuf win is intercepted. An unsatisfiable
        # `Accept` is left to the ordinary path rather than turned into a 406
        # here: this wrapper exists to add a format, not to start refusing
        # requests that worked yesterday.
        if isinstance(value, passthrough) or not isinstance(value, annotation):
            return value
        chosen = negotiate(request.header("accept"), offers)
        if chosen is None or chosen.media_type != PROTOBUF.media_type:
            return value
        response = Response(chosen.encode(value), media_type=media)
        # As `serialize` does: a shared cache must key on the format, or one
        # client's protobuf is served to another client's JSON request.
        response.headers.append((b"vary", b"Accept"))
        return response

    negotiated: Handler
    if inspect.iscoroutinefunction(handler):

        async def negotiated(request: Request) -> Any:  # type: ignore[no-redef]
            return _negotiated(request, await handler(request))

    else:

        def negotiated(request: Request) -> Any:  # type: ignore[no-redef]
            return _negotiated(request, handler(request))

    negotiated.__name__ = getattr(handler, "__name__", "negotiated")
    negotiated.__qualname__ = getattr(handler, "__qualname__", "negotiated")
    return negotiated


# Resolved field specs per dataclass: (name, annotation, required). Type-hint
# evaluation is expensive; it must happen once per class, never per request.
_DATACLASS_SPECS: dict[type, tuple[tuple[str, Any, bool], ...]] = {}
_DATACLASS_WIRE_SPECS: dict[type, tuple[tuple[str, str, Any, bool], ...]] = {}


def _dataclass_spec(cls: type) -> tuple[tuple[str, Any, bool], ...]:
    spec = _DATACLASS_SPECS.get(cls)
    if spec is None:
        label = f"body model {getattr(cls, '__qualname__', cls)!s}"
        hints = _resolve_hints(cls, label, extras=False)
        spec = tuple(
            (field.python_name, field.annotation, field.required)
            for field in dataclass_field_image(cls, hints, fallback=Any)
        )
        _DATACLASS_SPECS[cls] = spec
    return spec


def _dataclass_wire_spec(cls: type) -> tuple[tuple[str, str, Any, bool], ...]:
    spec = _DATACLASS_WIRE_SPECS.get(cls)
    if spec is None:
        label = f"body model {getattr(cls, '__qualname__', cls)!s}"
        hints = _resolve_hints(cls, label, extras=True)
        entries: list[tuple[str, str, Any, bool]] = []
        wire_names: set[str] = set()
        for field in dataclass_field_image(cls, hints, fallback=Any):
            annotation = field.annotation
            _base, metadata = _field_annotation(annotation)
            wire_name = (
                metadata.alias if metadata is not None and metadata.alias else field.python_name
            )
            if wire_name in wire_names:
                raise TypeError(f"body model {cls.__qualname__} maps two fields to {wire_name!r}")
            wire_names.add(wire_name)
            entries.append(
                (
                    field.python_name,
                    wire_name,
                    annotation,
                    field.required,
                )
            )
        spec = tuple(entries)
        _DATACLASS_WIRE_SPECS[cls] = spec
    return spec


def _validate_dataclass(
    cls: Any, value: Any, loc: tuple[Any, ...], errors: list, budget: list
) -> Any:
    if isinstance(value, cls):
        return value
    if not isinstance(value, dict):
        errors.append(_error(loc, "value is not an object", "dict"))
        return value
    kwargs: dict[str, Any] = {}
    spec = _dataclass_wire_spec(cls)
    for name, wire_name, annotation, required in spec:
        if wire_name in value:
            kwargs[name] = _validate(
                annotation, value[wire_name], (*loc, wire_name), errors, budget
            )
        elif required:
            errors.append(_error((*loc, wire_name), "field is required", "missing"))
    if len(value) > len(kwargs):
        known = {wire_name for _name, wire_name, _annotation, _required in spec}
        # Insertion order (not set-difference order) so the error list is
        # deterministic and matches the native validator byte-for-byte.
        for extra in value:
            if extra not in known:
                errors.append(_error((*loc, extra), "unexpected field", "extra"))
    # A negative budget means validation was cut short mid-tree, so kwargs may
    # be incomplete -- never construct from a truncated body.
    if errors or budget[0] < 0:
        return value
    return cls(**kwargs)


# --- scalar (path/query) conversion ----------------------------------------------

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _convert_scalar(annotation: Any, raw: str, loc: tuple[Any, ...]) -> Any:
    if annotation is str or annotation is Any or annotation is inspect.Parameter.empty:
        return raw
    if annotation is int:
        try:
            return int(raw)
        except ValueError:
            raise ValidationError([_error(loc, f"{raw!r} is not an integer", "int")]) from None
    if annotation is float:
        try:
            return float(raw)
        except ValueError:
            raise ValidationError([_error(loc, f"{raw!r} is not a number", "float")]) from None
    if annotation is bool:
        lowered = raw.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValidationError([_error(loc, f"{raw!r} is not a boolean", "bool")])
    if annotation is Instant or annotation is _datetime.datetime:
        # `datetime` is accepted alongside `Instant` because ported handlers
        # annotate it, and answering `unsupported annotation` to those would be
        # unhelpful. Both land on an aware `Instant`: a query string without an
        # offset is refused rather than read as UTC, which is the mistake this
        # type exists to make impossible.
        try:
            return Instant.parse(raw)
        except TemporalError as error:
            raise ValidationError([_error(loc, str(error), "instant")]) from None
    if annotation is _datetime.date:
        try:
            return _datetime.date.fromisoformat(raw)
        except ValueError:
            raise ValidationError(
                [_error(loc, f"{raw!r} is not an ISO-8601 date", "date")]
            ) from None
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        options = [o for o in typing.get_args(annotation) if o is not _NONE_TYPE]
        if len(options) == 1:
            return _convert_scalar(options[0], raw, loc)
    raise ValidationError(
        [_error(loc, f"unsupported parameter annotation {annotation!r}", "unsupported")]
    )


ScalarConstraint = tuple[Any, Any, str]  # (minimum, maximum, overflow)


def _apply_constraint(value: Any, constraint: ScalarConstraint, loc: tuple[Any, ...]) -> Any:
    """Clamp or reject a converted numeric `value` against a range.

    Runs only for values that parsed cleanly (invalid syntax already raised in
    :func:`_convert_scalar`) and only when the parameter actually supplied a
    value — a missing parameter keeps the handler default untouched.
    """
    minimum, maximum, overflow = constraint
    if minimum is not None and value < minimum:
        if overflow == "clamp":
            return minimum
        raise ValidationError([_error(loc, f"value must be >= {minimum}", "minimum")])
    if maximum is not None and value > maximum:
        if overflow == "clamp":
            return maximum
        raise ValidationError([_error(loc, f"value must be <= {maximum}", "maximum")])
    return value


# --- explicit request sources and dependency injection ----------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Path:
    """Bind a parameter to a path placeholder.

    Written in `Annotated`, never as a default — `Annotated[int, Path()]`. A
    parameter whose name already matches a placeholder binds without this
    marker; `Path("item_id")` is what lets the handler call it something else.

    A path parameter is always required and its handler default is never
    consulted, because the router only matches a request that supplied every
    placeholder. Naming a placeholder the route path does not contain is a
    `TypeError` when routes compile, not a 404 at request time. A value that
    does not convert to the annotated type fails with 422 and a `loc` of
    `["path", name]`.

    Args:
        alias: Placeholder name to read, when it differs from the parameter name.
    """

    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Query:
    """Bind a parameter to a query-string value, optionally with a numeric range.

    Written in `Annotated`, never as a default — `Annotated[int, Query()] = 20`.
    Writing `limit: int = Query(20)`, which is FastAPI's spelling, is a
    `TypeError` when routes compile: it binds nothing, so accepting it would
    ignore the bounds below and hand the `Query` object itself to the handler as
    the value. Every marker below refuses the same way.

    A repeated key takes its first value. An absent parameter falls back to the
    handler default before any bound is considered; with no default it fails
    with 422 and a `loc` of `["query", name]`. Invalid syntax stays an error
    whatever `overflow` says, because a range applies only to a value that
    parsed. `minimum` and `maximum` apply only to an `int` or `float` parameter;
    a range on any other annotation is a `TypeError` when routes compile.

    Args:
        alias: Query key to read, when it differs from the parameter name.
        minimum: Inclusive lower bound. None leaves the value unbounded below.
        maximum: Inclusive upper bound. None leaves the value unbounded above.
        overflow: "error" for a 422 on an out-of-range value, "clamp" to pin it.

    Raises:
        ValueError: overflow is neither "error" nor "clamp", or minimum exceeds maximum.
    """

    alias: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    overflow: str = "error"

    def __post_init__(self) -> None:
        if self.overflow not in ("error", "clamp"):
            raise ValueError(f"Query overflow must be 'error' or 'clamp', got {self.overflow!r}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"Query minimum {self.minimum!r} exceeds maximum {self.maximum!r}")


@dataclasses.dataclass(frozen=True, slots=True)
class Header:
    """Bind a parameter to a request header.

    Written in `Annotated`, never as a default — `Annotated[str, Header()]`.
    The lookup is case-insensitive and takes the first value when a header
    repeats. **The name is used literally.** Unlike FastAPI, wreath does not
    rewrite `user_agent` into `user-agent`, so any header whose name is not a
    Python identifier needs an explicit alias.

    An absent header falls back to the handler default; with no default the
    request fails with 422 and a `loc` of `["header", name]`.

    Args:
        alias: Header name to read, when it differs from the parameter name.
    """

    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Cookie:
    """Bind a parameter to a request cookie.

    Written in `Annotated`, never as a default — `Annotated[str, Cookie()]`.
    Cookie names are matched exactly, case included, and the name is used
    literally — no underscore-to-hyphen rewriting. An absent cookie falls back
    to the handler default; with no default the request fails with 422 and a
    `loc` of `["cookie", name]`.

    Reading any cookie parses the whole Cookie header, which is refused with a
    431 when it exceeds the configured `max_cookie_bytes`.

    Args:
        alias: Cookie name to read, when it differs from the parameter name.
    """

    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Body:
    """Bind a parameter to the whole decoded JSON request body.

    Written in `Annotated`, never as a default — `Annotated[list[int], Body()]`.
    A dataclass or mapped ORM model already binds the body without any marker;
    `Body()` is what makes a non-dataclass annotation — a list, a dict, a scalar
    — the body rather than a query parameter. The payload is checked against the
    annotation by a validator compiled once at route-compile time, so a handler
    annotated with a dataclass or model receives a constructed instance.

    A handler declares at most one body parameter and cannot combine a body with
    `Form()` or `File()` parameters; either is a `TypeError` when routes compile.
    A body that is not valid JSON is a 400, not a 422, because nothing could be
    validated against the annotation.

    Args:
        alias: Accepted for symmetry and ignored — a body has no name to look up.
    """

    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Form:
    """Bind a parameter to a multipart or urlencoded form field.

    Written in `Annotated`, never as a default — `Annotated[str, Form()]`. A
    scalar-annotated parameter takes one form field, converted to the annotated
    type. A parameter annotated with a dataclass or a mapped ORM model instead
    binds that whole model from the form fields and runs the same validator a
    JSON body would use, so a form-posted model accepts exactly the shapes a
    JSON-posted one does. A whole-model form rejects any field the model does
    not declare, and ignores `alias`.

    File parts are never model fields — bind an upload with a separate `File()`
    parameter. A handler declares at most one form model, cannot mix a form
    model with individual `Form()` fields, and cannot combine either with a body
    parameter; each is a `TypeError` when routes compile. A missing field with
    no handler default fails with 422 and a `loc` of `["form", name]`.

    Args:
        alias: Form field name to read, when it differs from the parameter name.
    """

    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class File:
    """Bind a parameter to an uploaded multipart file part.

    Written in `Annotated`, never as a default —
    `Annotated[UploadedFile, File()]`. The handler always receives a
    `wreath.request.UploadedFile`; the annotation documents intent and is never
    used to convert or validate the part. A multipart part carrying no filename
    is a form field, not a file, and binds with `Form()` instead.

    A missing upload with no handler default fails with 422 and a `loc` of
    `["file", name]`.

    Args:
        alias: Part name to read, when it differs from the parameter name.
    """

    alias: str | None = None


_SOURCE_MARKERS = (Path, Query, Header, Cookie, Body, Form, File)


class Depends:
    """Marks a handler parameter as provided by a dependency callable.

    Alone among the markers, `Depends` is written as the parameter's *default* —
    `session = Depends(get_session)` — not inside `Annotated`.

    ```python
    async def get_session(request):
        session = Session()
        try:
            yield session
        finally:
            await session.close()

    @app.get("/items")
    async def items(request, session = Depends(get_session)):
        ...
    ```

    The callable takes `request` first, plus its own `Depends` parameters,
    resolved recursively. Every other parameter of a dependency must be a
    `Depends` or carry a default, and a cycle in the graph is a `TypeError`
    naming the cycle when routes compile. A plain function, a coroutine
    function, and an async generator function are all accepted; a synchronous
    dependency runs inline on the event loop and is never moved to a thread, so
    a blocking one blocks the request. An async generator yields the value and
    is resumed once for cleanup after the handler returns — on success, on
    error, and on cancellation alike — innermost first. A plain, non-async
    generator gets no cleanup: its generator object is handed to the handler
    as the value.

    Dependencies resolve per request, after path, query, header, cookie and body
    binding and after any injected connection or ORM session. With `use_cache`
    (the default) a callable resolves at most once per request, keyed by
    callable *and* scope, so a diamond in the graph costs one call rather than
    two, and the same factory used at both scopes stays two distinct values.

    `scope="request"`, the default, resolves per request and cleans up when the
    handler returns. `scope="app"` resolves once, on first use, and shares that
    value with every later request; an async generator's cleanup then runs at
    lifespan shutdown instead. Use it for something expensive and stateless — a
    client, a compiled ruleset, a warmed lookup table. An app-scoped value is
    held by the application's `AppScope` whatever `use_cache` says; that flag
    governs only the per-request cache. App scope is reachable only through a
    route on a `Wreath` application — compiling one without a container is a
    `TypeError`.

    An app-scoped dependency may not depend on a request-scoped one: the value
    would outlive the request it was built from. That is a **compile-time**
    error, raised when routes compile, not a surprise on request 2.

    Args:
        fn: The dependency callable, taking `request` and its own dependencies.
        use_cache: Reuse one resolved value across a single request. Default True.
        scope: Lifetime of the value, "request" or "app". Default "request".

    Raises:
        ValueError: scope is neither "request" nor "app".
    """

    __slots__ = ("fn", "scope", "use_cache")

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        use_cache: bool = True,
        scope: str = "request",
    ) -> None:
        """Check `scope` here, where a typo fails at import, not per request."""
        if scope not in ("request", "app"):
            raise ValueError(f"scope must be 'request' or 'app', got {scope!r}")
        self.fn = fn
        self.scope = scope
        self.use_cache = use_cache


Resolver = Callable[[Request, dict, list], Awaitable[Any]]


class AppScope:
    """Values that outlive a request, owned by one application.

    Backs `Depends(..., scope="app")`. Explicitly owned rather than global: the
    container hangs off the `Wreath` instance, is passed into binder
    compilation, and is torn down by that app's lifespan shutdown. Two apps in
    one process do not share values. A new container is empty and constructs
    nothing until a request first asks for a value; `fn in scope` reports
    whether the dependency `fn` has resolved yet.

    First use resolves; later uses read. A burst of concurrent first requests
    constructs once, not once per request in flight.

    Single-flighting is **per key, not per container**. A container-wide lock
    deadlocks the moment one app-scoped dependency resolves another: the outer
    factory holds the lock and the inner one waits for it forever. Per-key
    in-flight futures let a nested chain resolve while still collapsing
    concurrent callers of the same key. (Recursion through the *same* key would
    still hang, but that is a dependency cycle, and `_compile_dep` rejects
    those at compile time.)
    """

    __slots__ = ("_cleanups", "_pending", "_values")

    def __init__(self) -> None:
        """Start empty: values, in-flight futures, and cleanups all accrue lazily."""
        self._values: dict[Callable[..., Any], Any] = {}
        self._pending: dict[Callable[..., Any], asyncio.Future[Any]] = {}
        self._cleanups: list[Any] = []

    def __contains__(self, fn: Callable[..., Any]) -> bool:
        return fn in self._values

    async def get_or_create(
        self, fn: Callable[..., Any], factory: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return the value held for `fn`, constructing it once if it is absent.

        Concurrent callers for the same key collapse onto a single `factory`
        call and all receive its result; callers for different keys never block
        each other, so one app-scoped dependency can resolve another without
        deadlocking. A `factory` that raises is not cached and its exception
        reaches every waiter, so the next request retries construction.

        Args:
            fn: Cache key — the dependency callable itself, not the factory.
            factory: Zero-argument coroutine function that builds the value.

        Returns:
            The value for `fn`, built by this call or by an earlier one.
        """
        # The warm path: one dict lookup, no lock, no future.
        try:
            return self._values[fn]
        except KeyError:
            pass
        pending = self._pending.get(fn)
        if pending is not None:
            # Someone else is building this key; wait on their result.
            # A cancelled waiter must not cancel the shared future and thereby
            # cancel every other first request waiting for the same singleton.
            return await asyncio.shield(pending)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[fn] = future
        try:
            value = await factory()
        except BaseException as error:
            # A failed construction is not cached: the next request retries.
            self._pending.pop(fn, None)
            if not future.done():
                future.set_exception(error)
            # Nobody may be awaiting it; keep the loop from reporting it.
            future.exception()
            raise
        self._values[fn] = value
        self._pending.pop(fn, None)
        if not future.done():
            future.set_result(value)
        return value

    def track_cleanup(self, generator: Any) -> None:
        """Register a started app-scoped async generator for shutdown cleanup.

        Called by the resolver for a `Depends(..., scope="app")` async generator
        once its first `yield` has produced the value. Cleanup is held on the
        container rather than on the request that happened to construct the
        value, because that request ends long before the value does; `aclose`
        resumes everything tracked here at lifespan shutdown.

        Args:
            generator: A started async generator awaiting its cleanup resume.
        """
        self._cleanups.append(generator)

    async def aclose(self) -> None:
        """Resume every app-scoped generator for cleanup, innermost first.

        Every leg runs even when one fails, so a single bad teardown cannot
        strand the rest. The first failure is re-raised once all have run.
        """
        failure: BaseException | None = None
        for generator in reversed(self._cleanups):
            try:
                await anext(generator)
            except StopAsyncIteration:
                continue
            except Exception as error:  # noqa: BLE001 - re-raised below
                failure = failure or error
                continue
            try:
                await generator.aclose()
            except Exception as error:  # noqa: BLE001 - re-raised below
                failure = failure or error
        self._cleanups.clear()
        self._values.clear()
        if failure is not None:
            raise failure


_COROUTINE_TYPE = types.CoroutineType


def _compile_dependency(
    fn: Callable[..., Any],
    seen: tuple,
    *,
    scope: str = "request",
    app_scope: AppScope | None = None,
) -> Resolver:
    # `seen` is accepted for the existing call sites. Compilation memoizes a
    # resolver per callable and tracks the active path in a set, so a shared
    # dependency DAG compiles each callable once (not once per path, which is
    # exponential) and a depth-D chain does O(D) membership work (not O(D^2)).
    return _compile_dep(fn, {}, set(), [], scope, app_scope)


def _compile_dep(
    fn: Callable[..., Any],
    memo: dict[tuple[Callable[..., Any], str], Resolver],
    active: set[Callable[..., Any]],
    active_order: list[Callable[..., Any]],
    scope: str = "request",
    app_scope: AppScope | None = None,
) -> Resolver:
    # Memoized per (callable, scope): the same factory may legitimately appear
    # both request- and app-scoped, and the two compile to different resolvers.
    cached = memo.get((fn, scope))
    if cached is not None:
        return cached
    if fn in active:
        cycle = " -> ".join(repr(node) for node in (*active_order[active_order.index(fn) :], fn))
        raise TypeError(f"circular dependency through {fn!r}: {cycle}")
    active.add(fn)
    active_order.append(fn)
    try:
        try:
            parameters = list(inspect.signature(fn).parameters.values())
        except TypeError, ValueError:
            parameters = []
        # A dependency's own parameters are never bound from the request: only a
        # handler signature is inspected, and `_construct` calls
        # `fn(request, **kwargs)` with `kwargs` holding nested `Depends` values
        # and nothing else. A source marker here therefore does nothing, and the
        # two ways it fails are both silent-to-the-caller. On the *first*
        # parameter -- a callable written without a leading `request` -- the
        # request object is handed in as that value, and the first arithmetic on
        # it is a 500. On a later parameter the Python default wins, so the
        # handler is served stale defaults while `?page=2` is ignored, which is
        # a wrong answer with a 200 on it. Refused here, at route-compile time,
        # naming the two spellings that do work.
        try:
            dep_hints = typing.get_type_hints(fn, include_extras=True)
        except NameError, TypeError:
            dep_hints = {}
        for index, parameter in enumerate(parameters):
            annotation = dep_hints.get(parameter.name, parameter.annotation)
            if typing.get_origin(annotation) is not typing.Annotated:
                continue
            marker = next(
                (
                    item
                    for item in typing.get_args(annotation)[1:]
                    if isinstance(item, _SOURCE_MARKERS)
                ),
                None,
            )
            if marker is None:
                continue
            label = getattr(fn, "__qualname__", fn)
            where = (
                "its first parameter, which wreath fills with the request itself"
                if index == 0
                else "a parameter wreath never binds"
            )
            raise TypeError(
                f"dependency {label!s} declares {type(marker).__name__}() on "
                f"{parameter.name!r} -- {where}. A dependency is called as "
                f"fn(request, **nested_depends); request values are not bound "
                f"into it. Either take the request first and read it "
                f"(request.query_params), or declare {parameter.name!r} on the "
                f"handler instead and pass the value in."
            )
        nested: list[tuple[str, Depends, Resolver]] = []
        for parameter in parameters[1:]:
            default = parameter.default
            if isinstance(default, Depends):
                # A lifetime inversion is caught here, at compile time. An
                # app-scoped value built from a request-scoped one would
                # capture the first request that happened to construct it and
                # hand it to every caller afterwards -- a data-leak shape, not
                # just a bug, so it must never reach a request.
                if scope == "app" and default.scope != "app":
                    raise TypeError(
                        f"app-scoped dependency {fn!r} cannot depend on "
                        f"{default.scope}-scoped {default.fn!r}: the value would "
                        "outlive the request it was built from. Mark the inner "
                        "dependency scope='app', or make the outer one "
                        "request-scoped."
                    )
                # An app-scoped dependency nested under a request-scoped one is
                # fine, and compiles in its own scope.
                nested.append(
                    (
                        parameter.name,
                        default,
                        _compile_dep(
                            default.fn,
                            memo,
                            active,
                            active_order,
                            default.scope,
                            app_scope,
                        ),
                    )
                )
            elif default is inspect.Parameter.empty:
                raise TypeError(
                    f"dependency {fn!r} parameter {parameter.name!r} must be a "
                    "Depends or have a default"
                )
    finally:
        active.discard(fn)
        active_order.pop()
    is_async_gen = inspect.isasyncgenfunction(fn)
    is_coroutine = inspect.iscoroutinefunction(fn)
    # Keyed by (callable, scope): the same factory used at both scopes is two
    # different values with two different lifetimes and must not collide in one
    # request's cache. Both halves are fixed when the graph compiles, so the key
    # is built here rather than rebuilt for every dependency of every request.
    nested_plan = tuple(
        (name, (marker.fn, marker.scope), marker.use_cache, resolver)
        for name, marker, resolver in nested
    )

    async def _construct(request: Request, cache: dict, cleanups: list) -> Any:
        kwargs: dict[str, Any] = {}
        for name, key, use_cache, resolver in nested_plan:
            if use_cache and key in cache:
                kwargs[name] = cache[key]
            else:
                value = await resolver(request, cache, cleanups)
                if use_cache:
                    cache[key] = value
                kwargs[name] = value
        if is_async_gen:
            generator = fn(request, **kwargs)
            value = await anext(generator)
            cleanups.append(generator)
            return value
        result = fn(request, **kwargs)
        if is_coroutine or _awaitable(result):
            return await result
        return result

    if scope == "app":
        if app_scope is None:
            raise TypeError(
                f"app-scoped dependency {fn!r} was compiled without an "
                "application scope container; app scope is only available to "
                "dependencies reached through a route on a Wreath application"
            )
        container = app_scope

        async def resolve(request: Request, cache: dict, cleanups: list) -> Any:
            # Cleanup is tracked on the container, not the request: an
            # app-scoped generator is resumed at lifespan shutdown, so it must
            # not be handed the per-request cleanup list.
            async def factory() -> Any:
                app_cleanups: list[Any] = []
                # Construction happens once, so a slow singleton would otherwise
                # be invisible -- charged to whichever unlucky request built it.
                # The marker read is one ContextVar.get on an armed request and
                # nothing at all on the warm path, which never gets here.
                marker = _phase_marker.get(None)
                started = _monotonic_ns() if marker is not None else 0
                value = await _construct(request, {}, app_cleanups)
                if marker is not None:
                    marker(_PH_DI_CONSTRUCT, 0, _COV_PYTHON, _monotonic_ns() - started)
                for generator in app_cleanups:
                    container.track_cleanup(generator)
                return value

            return await container.get_or_create(fn, factory)
    else:
        resolve = _construct

    memo[(fn, scope)] = resolve
    return resolve


# --- binder compilation -----------------------------------------------------------


def _is_model(annotation: Any) -> bool:
    """True for a mapped wreath.orm model, which can validate a body itself."""
    if not isinstance(annotation, type):
        return False
    try:
        from .orm.model import Model
    except ImportError:
        return False
    return issubclass(annotation, Model) and annotation.__wreath_table__ is not None


class _BodyValidator:
    __slots__ = ("_decode_json", "_validate")

    def __init__(
        self,
        validate_fn: Callable[[Any, tuple[Any, ...]], Any],
        decode_json: Callable[[bytes, tuple[Any, ...]], Any] | None = None,
    ) -> None:
        self._validate = validate_fn
        self._decode_json = decode_json

    def __call__(self, payload: Any, loc: tuple[Any, ...]) -> Any:
        return self._validate(payload, loc)

    def decode_json_validation_tape(self, data: bytes, loc: tuple[Any, ...]) -> Any:
        decode_json = self._decode_json
        if decode_json is not None:
            return decode_json(data, loc)
        return self._validate(_json_loads(data), loc)


class _MultipartValidationTape:
    __slots__ = ("_fields", "_files")

    def __init__(self, fields: tuple[Any, ...], files: tuple[Any, ...]) -> None:
        self._fields = fields
        self._files = files

    async def decode_multipart_validation_tape(
        self, request: Request, kwargs: dict[str, Any]
    ) -> None:
        parsed = await request.form()
        for name, alias, annotation, default in self._fields:
            raw = parsed.fields.get(alias)
            if raw is None:
                if default is inspect.Parameter.empty:
                    raise ValidationError([_error(("form", alias), "field is required", "missing")])
                kwargs[name] = default
            else:
                kwargs[name] = _convert_scalar(annotation, raw, ("form", alias))
        for name, alias, _annotation, default in self._files:
            upload = parsed.files.get(alias)
            if upload is None:
                if default is inspect.Parameter.empty:
                    raise ValidationError([_error(("file", alias), "file is required", "missing")])
                kwargs[name] = default
            else:
                kwargs[name] = upload


def _unwrap_form_type(annotation: Any) -> Any:
    """Peel `Mapped[T]` and `Optional[T]` down to the scalar type that
    :func:`_convert_scalar` understands; leave anything else unchanged."""
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        options = [o for o in typing.get_args(annotation) if o is not _NONE_TYPE]
        if len(options) == 1:
            return _unwrap_form_type(options[0])
        return annotation
    args = typing.get_args(annotation)
    name = getattr(origin, "__name__", "") or getattr(annotation, "__name__", "")
    if name == "Mapped" and args:  # wreath.orm Mapped[T]
        return _unwrap_form_type(args[0])
    return annotation


def _form_model_fields(annotation: Any) -> tuple[tuple[str, Any, bool], ...]:
    """`(field_name, python_type, required)` for each scalar field of a
    dataclass or ORM model bound from multipart form fields. File parts are not
    model fields — bind an upload with a separate `File()` parameter."""
    label = f"form model {getattr(annotation, '__qualname__', annotation)!s}"
    try:
        hints = _resolve_hints(annotation, label, extras=False)
    except (TypeError, ValueError) as error:
        if error.__cause__ is not None:  # an unresolvable name, diagnosed
            raise
        hints = {}
    if _is_model(annotation):
        model_fields: list[tuple[str, Any, bool]] = []
        for column in annotation.__wreath_columns__:
            name = column.python_name
            ptype = _unwrap_form_type(hints.get(name, str))
            required = not (column.nullable or column.server_default or column.primary_key)
            model_fields.append((name, ptype, required))
        return tuple(model_fields)
    result: list[tuple[str, Any, bool]] = []
    for field in dataclass_field_image(annotation, hints):
        ptype = _unwrap_form_type(field.annotation)
        result.append((field.python_name, ptype, field.required))
    return tuple(result)


class _FormModelValidationTape:
    """Bind a whole dataclass/ORM model from multipart form fields, then run the
    SAME native body validator (the JSON-body path) over the assembled dict — so
    a form-posted model is validated exactly like a JSON-posted one. Reuses the
    native multipart parser (`request.form()`) and the native validation tape;
    no new native code. File parts are bound by separate `File()` params.

    That reuse is also why there is nothing to accelerate here: both pieces it
    stands on are already C (`_body_validator` and `request.form()`), and what
    is left is glue.
    """

    __slots__ = ("_name", "_fields", "_validator")

    def __init__(
        self,
        name: str,
        fields: tuple[tuple[str, Any, bool], ...],
        validator: _BodyValidator,
    ) -> None:
        self._name = name
        self._fields = fields
        self._validator = validator

    async def decode(self, request: Request, kwargs: dict[str, Any]) -> None:
        parsed = await request.form()
        known = {name for name, _type, _required in self._fields}
        extras = [key for key in parsed.fields if key not in known]
        if extras:
            raise ValidationError(
                [_error(("form", key), "unexpected field", "extra") for key in extras]
            )
        payload: dict[str, Any] = {}
        for name, ptype, _required in self._fields:
            raw = parsed.fields.get(name)
            if raw is None:
                continue  # absent: the body validator applies default / flags missing
            payload[name] = _convert_scalar(ptype, raw, ("form", name))
        kwargs[self._name] = self._validator(payload, ("form",))


def _protobuf_requested(request: Request) -> bool:
    """Whether this request's `Content-Type` asks for the protobuf decoder.

    The set of spellings lives in `wreath.negotiation` beside the serializer
    that emits one of them, so the request half and the response half cannot
    disagree about what protobuf is called.
    """
    header = request.header("content-type")
    if header is None:
        return False
    return header.split(";")[0].strip().lower() in _PROTOBUF_MEDIA_TYPES


def _decode_protobuf_body(annotation: Any, body: bytes) -> Any:
    """Read a protobuf request body into the handler's declared message.

    **Unknown fields are preserved, where an unknown JSON name is refused.** The
    same handler, the same declared shape, two strictnesses chosen by
    `Content-Type` -- and that asymmetry is the decision rather than an
    accident, so it is stated here as well as in the guide:

    * An unexpected **name** in a JSON object is almost always a typo, and the
      sender has no way to have meant it. Refusing tells them which one.
    * An unexpected **number** on a protobuf wire is a peer compiled against a
      newer `.proto`. That is the situation field numbers exist for, and
      tolerating it is the mechanism -- `wreath.protobuf` keeps the bytes and
      `encode` puts them back, so a service that reads, edits and forwards a
      message does not strip what a newer peer sent through it. Refusing would
      make every schema rollout a synchronised deploy of every consumer.

    The refusals below are three different sentences on purpose: "your bytes are
    not protobuf" and "this endpoint's body is not a declared message" have
    different remedies, and one message covering both is a message that tells
    nobody which happened.

    Raises:
        BadRequest: the annotation is not a `@message`, or the bytes are not a
            readable encoding of it.
    """
    if not _is_message(annotation):
        raise BadRequest(
            f"this endpoint's body is {getattr(annotation, '__name__', annotation)!s}, "
            "which is not a @message: protobuf carries field numbers rather than "
            "names, so there is nothing to read the wire against. Declare the "
            "body with @message from wreath.protobuf, or send JSON."
        )
    try:
        return _protobuf_decode(annotation, body)
    except _ProtobufDecodeError as exc:
        raise BadRequest(f"invalid protobuf body: {exc}") from None


def _body_validator(annotation: Any) -> _BodyValidator:
    """Compile the body checker once, at route-compile time.

    A model validates a payload against its own columns in a single pass, so the
    values are proven once and land straight in the model's cells rather than
    being checked against a dataclass and then re-checked on assignment.
    """
    if _is_model(annotation):
        from .orm.validation import compile_model_validator

        return _BodyValidator(compile_model_validator(annotation))

    # Compile the annotation into a flat plan once, then check the whole body
    # in one call. Recursive dataclasses cannot be flattened and retain their
    # recursive annotation validator.
    try:
        plan = _compile_plan(annotation, frozenset())
    except _PlanUnsupported:
        plan = None
    if plan is not None:
        run_validation = _core.run_validation
        decode_json = _core.decode_json_validation_tape

        def checked(result_and_errors: tuple[Any, list[Any]]) -> Any:
            result, errors = result_and_errors
            if errors:
                raise ValidationError(errors)
            return result

        def planned_validate(payload: Any, loc: tuple[Any, ...]) -> Any:
            return checked(run_validation(plan, payload, loc))

        def planned_decode(data: bytes, loc: tuple[Any, ...]) -> Any:
            return checked(decode_json(data, plan, loc))

        return _BodyValidator(planned_validate, planned_decode)

    def validate_annotation(payload: Any, loc: tuple[Any, ...]) -> Any:
        return validate(annotation, payload, loc)

    return _BodyValidator(validate_annotation)


def _path_placeholders(path: str) -> frozenset[str]:
    names = set()
    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}") and len(segment) > 2:
            names.add(segment[1:-1].split(":", 1)[0])
    return frozenset(names)


@dataclasses.dataclass(frozen=True, slots=True)
class BindingSpec:
    """The compiled shape of a typed handler signature.

    Produced once by `inspect_handler` at route-compile time and consumed by
    `compile_binder`, by the OpenAPI generator, and by typegen — so a handler's
    signature is read once and three subsystems agree on what it said.

    Each scalar entry is `(parameter_name, wire_name, annotation)` for a path
    parameter and `(parameter_name, wire_name, annotation, default)` elsewhere,
    where `default` is `inspect.Parameter.empty` when the parameter is required.
    An annotation here is the base type with `Annotated` already stripped.

    Args:
        path_params: Placeholders bound from the matched path.
        query_params: Parameters bound from the query string.
        header_params: Parameters bound from request headers.
        cookie_params: Parameters bound from request cookies.
        form_params: Scalar parameters bound from individual form fields.
        file_params: Parameters bound to uploaded multipart file parts.
        body: The `(parameter_name, annotation)` bound from the JSON body, or None.
        returns: The handler's return annotation, or `inspect.Parameter.empty`.
        depends: `(parameter_name, Depends)` for each injected dependency.
        connections: `(parameter_name, FromDatabase)` for each injected connection.
        sessions: `(parameter_name, FromORM)` for each injected ORM session.
        query_constraints: `(parameter_name, (minimum, maximum, overflow))` ranges.
        form_model: The `(parameter_name, model)` bound whole from a form, or None.
    """

    path_params: tuple[tuple[str, str, Any], ...]
    query_params: tuple[tuple[str, str, Any, Any], ...]
    header_params: tuple[tuple[str, str, Any, Any], ...]
    cookie_params: tuple[tuple[str, str, Any, Any], ...]
    form_params: tuple[tuple[str, str, Any, Any], ...]
    file_params: tuple[tuple[str, str, Any, Any], ...]
    body: tuple[str, Any] | None
    returns: Any
    depends: tuple[tuple[str, Any], ...] = ()  # (name, Depends)
    connections: tuple[tuple[str, Any], ...] = ()  # (name, FromDatabase)
    sessions: tuple[tuple[str, Any], ...] = ()  # (name, FromORM)
    # Compiled numeric ranges keyed by parameter name; parallel to query_params
    # so OpenAPI/typegen keep iterating the 4-tuple shape unchanged.
    query_constraints: tuple[tuple[str, ScalarConstraint], ...] = ()
    # A dataclass/ORM model bound whole-cloth from multipart form fields
    # (``Annotated[MyModel, Form()]``) — the ``as_form`` ergonomic.
    form_model: tuple[str, Any] | None = None


def _blame_unresolvable(obj: Any, label: str, error: NameError) -> str:
    """Name the parameter whose annotation could not be resolved.

    `get_type_hints` evaluates every annotation together and reports only the
    first name that failed, so its `NameError` says nothing about *which*
    parameter carried it. Resolving each annotation on its own attributes the
    failure, and costs nothing on the path that matters: this runs only after
    the whole-signature attempt has already raised.
    """
    globalns = getattr(_sys.modules.get(getattr(obj, "__module__", ""), None), "__dict__", {})
    for name, annotation in getattr(obj, "__annotations__", {}).items():
        probe = types.SimpleNamespace(__annotations__={name: annotation})
        try:
            typing.get_type_hints(probe, globalns, None, include_extras=True)
        except NameError:
            return f"{label} annotates {name!r} with an unresolvable name"
    # The whole-signature call failed but no single annotation reproduces it --
    # a forward reference that only resolves in combination. Blame the callable
    # rather than guessing at a parameter.
    return f"{label} carries an annotation with an unresolvable name"


def _resolve_hints(obj: Any, label: str, *, extras: bool = True) -> dict[str, Any]:
    """Resolve `obj`'s annotations, or refuse naming what could not be resolved.

    Annotations are evaluated once, when the route table compiles, in the module
    the callable was defined in. A name visible only inside a function body, or
    imported under `if TYPE_CHECKING:`, is not in that namespace -- and the bare
    `NameError` names neither the callable nor the parameter, so a startup
    failure arrives with nothing to act on. Refused here with all three facts,
    per the refuse-rather-than-half-wire rule in `AGENTS.md`.
    """
    try:
        return typing.get_type_hints(obj, include_extras=extras)
    except NameError as error:
        raise TypeError(
            f"{_blame_unresolvable(obj, label, error)}: {error}. Annotations are "
            "resolved at route-compile time in the module the callable was "
            "defined in, so a name local to a function or imported only under "
            "`if TYPE_CHECKING:` is not visible. Import it at module scope, or "
            "annotate with a type that is."
        ) from error


def _return_annotation(handler: Any) -> Any:
    """The handler's resolved return annotation, or `Parameter.empty`.

    Lives here rather than beside the OpenAPI and typegen code that also calls
    it, because `app.py` needs it to compile a route and importing it from
    `typegen.inspect` pulled the whole code-generation package -- the type
    model, the renderers and the TypeScript target -- into the import path of
    every application that only ever serves requests. 13ms of a 207ms
    `import wreath`, for six lines needing nothing but `typing` and `inspect`.

    Unlike `_resolve_hints` this swallows the failure: a handler whose return
    annotation will not resolve still binds and still serves, it just does not
    get response validation or a documented response schema. Refusing here
    would turn a documentation problem into a boot failure.
    """
    try:
        hints = typing.get_type_hints(handler, include_extras=False)
    except TypeError, ValueError, NameError:
        return inspect.Parameter.empty
    return hints.get("return", inspect.Parameter.empty)


def inspect_handler(handler: Handler, path: str, host: str | None = None) -> BindingSpec | None:
    """Read a handler signature once and resolve every parameter to a source.

    Called at route-compile time, never per request. Markers in `Annotated`
    decide the source when present; otherwise a parameter matching a path
    placeholder is a path parameter, a dataclass or mapped ORM model annotation
    is the JSON body, and everything else is a query parameter. The first
    parameter is always the request and is never bound.

    Every conflict a signature can express is refused here rather than at
    request time — `*args`/`**kwargs`, two body parameters, a body mixed with
    form or file parameters, a form model mixed with individual form fields, a
    numeric range on a non-numeric parameter, a `Path` alias absent from the
    route path, and a bare `Session` that does not say which registry it wants.

    Args:
        handler: The endpoint callable, whose first parameter is the request.
        path: The route path, including its `{placeholder}` segments.

    Returns:
        The spec, or None for a request-only handler or an uninspectable object.

    Raises:
        TypeError: The signature is self-contradictory or cannot be bound.
    """
    try:
        signature = inspect.signature(handler)
    except TypeError, ValueError:
        return None
    parameters = tuple(signature.parameters.values())
    if len(parameters) <= 1:
        return None
    label = f"handler {getattr(handler, '__qualname__', handler)!s}"
    try:
        hints = _resolve_hints(handler, label)
    except (TypeError, ValueError) as error:
        # A refusal from `_resolve_hints` carries the diagnosis and must reach
        # the caller; the bare TypeError/ValueError `get_type_hints` raises for
        # an uninspectable object still means "not a bindable signature".
        if error.__cause__ is not None:
            raise
        return None

    placeholders = _path_placeholders(path)
    if host is not None:
        placeholders = placeholders | _path_placeholders("/" + host.replace(".", "/"))
    path_specs: list[tuple[str, str, Any]] = []
    query_specs: list[tuple[str, str, Any, Any]] = []
    query_constraints: list[tuple[str, ScalarConstraint]] = []
    header_specs: list[tuple[str, str, Any, Any]] = []
    cookie_specs: list[tuple[str, str, Any, Any]] = []
    form_specs: list[tuple[str, str, Any, Any]] = []
    file_specs: list[tuple[str, str, Any, Any]] = []
    depends_specs: list[tuple[str, Depends]] = []
    connection_specs: list[tuple[str, Any]] = []
    session_specs: list[tuple[str, Any]] = []
    body_spec: tuple[str, Any] | None = None
    form_model_spec: tuple[str, Any] | None = None

    for parameter in parameters[1:]:
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            label = getattr(handler, "__qualname__", handler)
            raise TypeError(f"handler {label!s} cannot bind *args/**kwargs")
        annotation = hints.get(parameter.name, parameter.annotation)
        from .orm.session import FromORM, Session
        from .postgres import Connection, FromDatabase

        origin = typing.get_origin(annotation)
        annotated_args = typing.get_args(annotation) if origin is typing.Annotated else ()
        base_annotation = annotated_args[0] if annotated_args else annotation
        metadata = annotated_args[1:]
        database_marker = next((item for item in metadata if isinstance(item, FromDatabase)), None)
        orm_marker = next((item for item in metadata if isinstance(item, FromORM)), None)
        source = next((item for item in metadata if isinstance(item, _SOURCE_MARKERS)), None)
        alias = parameter.name if source is None or source.alias is None else source.alias
        default = parameter.default
        if isinstance(default, _SOURCE_MARKERS):
            # FastAPI's spelling, and the single most common porting mistake.
            # Accepting it was silently wrong three ways: nothing bound, the
            # marker's constraints were ignored, and the marker *object* was
            # handed to the handler as the parameter's value. Refused here, so it
            # is a startup error rather than a wrong value at request time.
            # `Depends` is deliberately not among `_SOURCE_MARKERS`: it is the
            # one marker that really is written as a default.
            label = getattr(handler, "__qualname__", handler)
            marker = type(default).__name__
            shown = (
                base_annotation.__name__
                if isinstance(base_annotation, type)
                and base_annotation is not inspect.Parameter.empty
                else "T"
            )
            raise TypeError(
                f"handler {label!s} parameter {parameter.name!r} uses "
                f"{marker}() as its default; wreath binds nothing from that. "
                f"Write the marker inside Annotated and leave the default an "
                f"ordinary Python default: "
                f"Annotated[{shown}, {marker}(...)] = <default>"
            )
        if any(isinstance(item, Depends) for item in metadata):
            # The mirror of the marker-as-default refusal above, and the worse
            # failure of the two. `Depends` is read from the default only, so
            # inside `Annotated` it was invisible: the parameter fell through to
            # the JSON body and a GET answered 400 "invalid JSON body". A 500
            # says *we* broke; a 400 says *you* broke, and the caller has no way
            # to tell it was a wiring bug on this side.
            label = getattr(handler, "__qualname__", handler)
            raise TypeError(
                f"handler {label!s} parameter {parameter.name!r} puts Depends() "
                f"inside Annotated; wreath reads Depends from the default only, "
                f"so nothing would be injected and the parameter would be bound "
                f"from the request body. Write it as the default instead: "
                f"{parameter.name} = Depends(...)"
            )
        if base_annotation is Session:
            if orm_marker is None:
                label = getattr(handler, "__qualname__", handler)
                raise TypeError(
                    f"handler {label!s} parameter {parameter.name!r} must be "
                    "Annotated[Session, FromORM(...)]; a bare Session does not say "
                    "which registry or workload it wants"
                )
            session_specs.append((parameter.name, orm_marker))
        elif base_annotation is Connection:
            connection_specs.append((parameter.name, database_marker or FromDatabase()))
        elif isinstance(default, Depends):
            depends_specs.append((parameter.name, default))
        elif isinstance(source, Path):
            if alias not in placeholders:
                raise TypeError(f"path parameter {alias!r} is not present in {path!r}")
            path_specs.append((parameter.name, alias, base_annotation))
        elif isinstance(source, Query):
            query_specs.append((parameter.name, alias, base_annotation, default))
            if source.minimum is not None or source.maximum is not None:
                if base_annotation not in (int, float):
                    label = getattr(handler, "__qualname__", handler)
                    raise TypeError(
                        f"handler {label!s} query parameter {alias!r} has a numeric "
                        f"range but is annotated {base_annotation!r}; minimum/maximum "
                        "apply only to int or float"
                    )
                query_constraints.append(
                    (parameter.name, (source.minimum, source.maximum, source.overflow))
                )
        elif isinstance(source, Header):
            header_specs.append((parameter.name, alias, base_annotation, default))
        elif isinstance(source, Cookie):
            cookie_specs.append((parameter.name, alias, base_annotation, default))
        elif isinstance(source, Body):
            if body_spec is not None:
                raise TypeError("handler declares two body parameters")
            body_spec = (parameter.name, base_annotation)
        elif isinstance(source, Form):
            if dataclasses.is_dataclass(base_annotation) or _is_model(base_annotation):
                if form_model_spec is not None:
                    label = getattr(handler, "__qualname__", handler)
                    raise TypeError(f"handler {label!s} declares two form-model parameters")
                form_model_spec = (parameter.name, base_annotation)
            else:
                form_specs.append((parameter.name, alias, base_annotation, default))
        elif isinstance(source, File):
            file_specs.append((parameter.name, alias, base_annotation, default))
        elif parameter.name in placeholders:
            path_specs.append((parameter.name, parameter.name, base_annotation))
        elif dataclasses.is_dataclass(base_annotation) or _is_model(base_annotation):
            if body_spec is not None:
                label = getattr(handler, "__qualname__", handler)
                raise TypeError(f"handler {label!s} declares two body parameters")
            body_spec = (parameter.name, base_annotation)
        else:
            query_specs.append((parameter.name, parameter.name, base_annotation, default))
    if body_spec is not None and (form_specs or file_specs or form_model_spec is not None):
        raise TypeError("a handler cannot combine body and form/file parameters")
    if form_model_spec is not None and form_specs:
        raise TypeError("a handler cannot combine a form-model with individual Form() fields")
    # Resolve the body and form models here rather than on the first request
    # that carries one. Both cache, so this is the same work moved earlier --
    # and an unresolvable annotation inside a model is a declaration error,
    # which belongs at compile time and not in a 500 the caller cannot place.
    if body_spec is not None and dataclasses.is_dataclass(body_spec[1]):
        _dataclass_wire_spec(body_spec[1])
    if form_model_spec is not None:
        _form_model_fields(form_model_spec[1])
    return BindingSpec(
        tuple(path_specs),
        tuple(query_specs),
        tuple(header_specs),
        tuple(cookie_specs),
        tuple(form_specs),
        tuple(file_specs),
        body_spec,
        hints.get("return", inspect.Parameter.empty),
        tuple(depends_specs),
        tuple(connection_specs),
        tuple(session_specs),
        tuple(query_constraints),
        form_model=form_model_spec,
    )


def compile_binder(
    handler: Handler,
    path: str,
    *,
    databases: Mapping[str, Any] | None = None,
    orm_registries: Mapping[str, Any] | None = None,
    dependencies: tuple[Depends, ...] = (),
    binding_spec: BindingSpec | None | object = _BINDING_SPEC_UNSET,
    app_scope: AppScope | None = None,
) -> Handler:
    """Wrap `handler` so its typed parameters are bound on each request.

    Everything expensive happens here, once: the signature is resolved, the body
    validator is compiled, dependency graphs are flattened into resolvers, and
    the database and ORM registry names are looked up. A request then only runs
    the resulting closure.

    A handler whose signature is exactly `(request)` and that carries no
    route-level dependencies is returned unchanged, so binding costs an
    unbound handler nothing. A handler that needs no connection, session, or
    dependency gets a leaner closure with no acquire/release machinery around
    it — that machinery was measured at roughly half the cost of a small
    request, which is why the distinction exists.

    Args:
        handler: The endpoint callable to wrap.
        path: The route path, including its `{placeholder}` segments.
        databases: Configured PostgreSQL databases by name, for `Connection` params.
        orm_registries: Configured ORM registries by name, for `Session` params.
        dependencies: Route-level dependencies resolved for their side effects only.
        binding_spec: A spec already computed by `inspect_handler`, to avoid redoing it.
        app_scope: The application's container for `scope="app"` dependencies.

    Returns:
        The wrapped handler, or the original when there is nothing to bind.

    Raises:
        TypeError: A named database is unknown, or a security_read connection was asked for.
    """
    spec = (
        inspect_handler(handler, path)
        if binding_spec is _BINDING_SPEC_UNSET
        else typing.cast(BindingSpec | None, binding_spec)
    )
    if spec is None and not dependencies:
        try:
            requestless = len(inspect.signature(handler).parameters) == 0
        except TypeError, ValueError:
            requestless = False
        if requestless:
            if inspect.iscoroutinefunction(handler):

                async def without_request(_request: Request) -> Any:
                    return await handler()

            else:

                def without_request(_request: Request) -> Any:
                    return handler()

            without_request.__name__ = getattr(handler, "__name__", "without_request")
            without_request.__qualname__ = getattr(handler, "__qualname__", "without_request")
            return without_request
        return handler
    path_specs = () if spec is None else spec.path_params
    path_opcodes = {str: 0, int: 1, float: 2, bool: 3}
    compiled_path_entries = (
        tuple((name, alias, path_opcodes[annotation]) for name, alias, annotation in path_specs)
        if all(annotation in path_opcodes for _name, _alias, annotation in path_specs)
        else None
    )
    compiled_path_plan = (
        (compiled_path_entries, tuple(name for name, _alias, _opcode in compiled_path_entries))
        if compiled_path_entries
        else None
    )
    query_specs = () if spec is None else spec.query_params
    query_constraints = {} if spec is None else dict(spec.query_constraints)
    # Each query parameter carries its own constraint, resolved here. The bind
    # loop used to ask `query_constraints.get(name)` per parameter per request
    # for a mapping that has not changed since compilation. `spec.query_params`
    # keeps its published shape, because OpenAPI and typegen read it.
    query_plan = tuple(
        (name, alias, annotation, default, query_constraints.get(name))
        for name, alias, annotation, default in query_specs
    )
    header_specs = () if spec is None else spec.header_params
    cookie_specs = () if spec is None else spec.cookie_params
    form_specs = () if spec is None else spec.form_params
    file_specs = () if spec is None else spec.file_params
    form_tape = _MultipartValidationTape(form_specs, file_specs)
    body_spec = None if spec is None else spec.body
    # Compiled here, never per request: a model body resolves to a validator
    # over its own columns.
    body_validator = None if body_spec is None else _body_validator(body_spec[1])
    form_model_spec = None if spec is None else spec.form_model
    form_model_tape = (
        None
        if form_model_spec is None
        else _FormModelValidationTape(
            form_model_spec[0],
            _form_model_fields(form_model_spec[1]),
            _body_validator(form_model_spec[1]),
        )
    )
    # Each entry carries its own cache key and cache flag, both fixed here. The
    # request loop below used to rebuild `(marker.fn, marker.scope)` and re-read
    # `marker.use_cache` per dependency per request for an answer that cannot
    # change after compilation.
    resolvers: tuple[tuple[str, tuple[Any, str], bool, Resolver], ...] = tuple(
        (
            name,
            (marker.fn, marker.scope),
            marker.use_cache,
            _compile_dependency(marker.fn, (), scope=marker.scope, app_scope=app_scope),
        )
        for name, marker in (() if spec is None else spec.depends)
    )
    side_effect_resolvers = tuple(
        (
            (marker.fn, marker.scope),
            marker.use_cache,
            _compile_dependency(marker.fn, (), scope=marker.scope, app_scope=app_scope),
        )
        for marker in dependencies
    )
    configured = databases or {}
    connections: list[tuple[str, Any, Any]] = []
    for name, marker in () if spec is None else spec.connections:
        if marker.workload == "security_read":
            raise TypeError("security_read connections cannot be injected into handlers")
        database_name = marker.name
        if database_name is None:
            if len(configured) != 1:
                raise TypeError(
                    "Connection injection requires FromDatabase when multiple "
                    "databases are configured"
                )
            database_name = next(iter(configured))
        try:
            database = configured[database_name]
        except KeyError:
            raise TypeError(f"unknown PostgreSQL database: {database_name}") from None
        connections.append((name, database, marker.workload))

    # One session per distinct (registry, workload), however many parameters
    # ask for it: two parameters naming the same pair share one unit of work.
    registries = orm_registries or {}
    sessions: list[tuple[str, tuple[str, str], Any, Any, Any]] = []
    if spec is not None and spec.sessions:
        from .orm.session import compile_session_binding

        for name, marker in spec.sessions:
            registry_name, registry = compile_session_binding(registries, marker)
            sessions.append(
                (name, (registry_name, marker.workload), registry, marker.workload, marker.tenant)
            )

    # Which machinery this handler actually needs is known here, not per
    # request. A handler that asks for no connection, session, or dependency
    # was still paying for all of it: four container allocations, a try/finally,
    # an unconditional `await _release([], [])`, and a `reversed()` over an empty
    # list. Measured at ~1.0us on a ~2.0us request (benchmarks/bench_scalar_
    # binding.py), against a ~0.06us floor -- larger than the scalar conversion
    # it surrounds, which is itself below that floor.
    needs_resources = bool(connections or sessions or resolvers or side_effect_resolvers)
    needs_body = bool(
        form_specs or file_specs or form_model_tape is not None or body_spec is not None
    )

    def _extract_scalars(request: Request, kwargs: dict[str, Any]) -> None:
        """Path, query, header, and cookie parameters. Synchronous by nature.

        **Fail-complete, like the body validator.** `?limit=nope&offset=nope`
        reports both, not the first -- a caller fixing a form one field per
        round trip was the only thing the old fail-fast shape bought, and the
        `errors` list has always been able to carry more than one.

        It costs nothing on the path that matters. `errors` is allocated only
        once something has already failed, and under CPython's zero-cost
        exceptions an untaken `try` is free -- so a request that binds cleanly
        does exactly the work it did before. The extra comparisons happen only
        on a request that was going to be a 422 anyway. Every conversion here is
        pure (parse a string, compare two numbers), so collecting them all has
        no side effect and no I/O; that is what makes continuing safe rather
        than merely cheap.
        """
        errors: list[dict[str, Any]] | None = None
        if compiled_path_entries:
            errors = _core.bind_path_into(
                request.path_params, compiled_path_entries, kwargs
            )
        else:
            for name, alias, annotation in path_specs:
                try:
                    kwargs[name] = _convert_scalar(
                        annotation, request.path_params[alias], ("path", alias)
                    )
                except ValidationError as invalid:
                    errors = invalid.errors if errors is None else [*errors, *invalid.errors]
        if query_specs:
            # `request.query_string`, not `request.scope[...]`: on the native
            # server the scope is a lazily materialized dict over
            # `_RequestContext`, so reading it here built the whole thing on
            # every bound handler purely to reach one member.
            query = parse_qs(request.query_string)
            # First occurrence wins, which `dict` gives from the pairs reversed:
            # the earliest pair is assigned last. `parse_qs` returns a list, so
            # `reversed` is a view rather than a copy and the whole fold is one
            # C loop -- it was a Python loop of `setdefault` calls, one per pair,
            # over pairs a C parser had just produced.
            values: dict[str, str] = dict(reversed(query))
            for name, alias, annotation, default, constraint in query_plan:
                raw = values.get(alias)
                if raw is None:
                    if default is inspect.Parameter.empty:
                        error = _error(("query", alias), "parameter is required", "missing")
                        errors = [error] if errors is None else [*errors, error]
                        continue
                    kwargs[name] = default
                else:
                    try:
                        converted = _convert_scalar(annotation, raw, ("query", alias))
                        if constraint is not None:
                            converted = _apply_constraint(converted, constraint, ("query", alias))
                    except ValidationError as invalid:
                        errors = invalid.errors if errors is None else [*errors, *invalid.errors]
                        continue
                    kwargs[name] = converted
        for name, alias, annotation, default in header_specs:
            raw = request.header(alias)
            if raw is None:
                if default is inspect.Parameter.empty:
                    error = _error(("header", alias), "parameter is required", "missing")
                    errors = [error] if errors is None else [*errors, error]
                    continue
                kwargs[name] = default
            else:
                try:
                    kwargs[name] = _convert_scalar(annotation, raw, ("header", alias))
                except ValidationError as invalid:
                    errors = invalid.errors if errors is None else [*errors, *invalid.errors]
        if cookie_specs:
            cookies = request.cookies
            for name, alias, annotation, default in cookie_specs:
                raw = cookies.get(alias)
                if raw is None:
                    if default is inspect.Parameter.empty:
                        error = _error(("cookie", alias), "parameter is required", "missing")
                        errors = [error] if errors is None else [*errors, error]
                        continue
                    kwargs[name] = default
                else:
                    try:
                        kwargs[name] = _convert_scalar(annotation, raw, ("cookie", alias))
                    except ValidationError as invalid:
                        errors = invalid.errors if errors is None else [*errors, *invalid.errors]
        if errors is not None:
            raise ValidationError(errors)

    async def _decode_body(request: Request, kwargs: dict[str, Any]) -> None:
        """Multipart, form-model, and JSON body parameters."""
        if form_specs or file_specs or form_model_tape is not None:
            # A body the multipart parser cannot read is the caller's fault, and
            # `Request.form` reports it as `ValueError` and leaves the status to
            # its caller -- which is here. Running these outside the handler the
            # JSON path already has made a bad boundary, an unterminated part, or
            # a malformed part header a 500 on every route binding `Form()` or
            # `File()`. `ValidationError` is not a `ValueError`, so a field that
            # is merely wrong still reaches the 422 it belongs in, and
            # `PayloadTooLarge` still surfaces as its own 413.
            try:
                if form_specs or file_specs:
                    await form_tape.decode_multipart_validation_tape(request, kwargs)
                if form_model_tape is not None:
                    await form_model_tape.decode(request, kwargs)
            except ValueError as exc:
                raise BadRequest(f"invalid form body: {exc}") from None
        if body_spec is not None and body_validator is not None:
            name, annotation = body_spec
            body = await request.body()
            # Which decoder reads the bytes is the client's to say. A `@message`
            # annotation does **not** mean protobuf-only: it is an ordinary
            # dataclass and bound from JSON before this branch existed, so
            # narrowing it here would have silently broken every handler that
            # already had one -- and OTLP/HTTP, the format's own reference
            # consumer, serves both encodings of one message behind one path.
            # See `_decode_protobuf_body` for the strictness that comes with it.
            if _protobuf_requested(request):
                kwargs[name] = _decode_protobuf_body(annotation, body)
            else:
                try:
                    kwargs[name] = body_validator.decode_json_validation_tape(body, ("body",))
                except ValueError as exc:
                    raise BadRequest(f"invalid JSON body: {exc}") from None

    async def bound_simple(request: Request) -> Any:
        """No connection, session, or dependency: nothing to lease or release."""
        kwargs: dict[str, Any] = {}
        _extract_scalars(request, kwargs)
        if needs_body:
            await _decode_body(request, kwargs)
        # A `def` handler is called, not awaited; see `docs/guides/routing.md`.
        # The binder itself has to stay `async` here because binding a body is
        # asynchronous, so the convention is decided per call rather than
        # compiled away as it is in `compile_response_validator`.
        result = handler(request, **kwargs)
        return await result if result.__class__ is _COROUTINE_TYPE else result

    async def bound(request: Request) -> Any:
        kwargs: dict[str, Any] = {}
        _extract_scalars(request, kwargs)
        if needs_body:
            await _decode_body(request, kwargs)
        cache: dict[Any, Any] = {}
        cleanups: list[Any] = []
        borrowed: list[tuple[Any, Any, str]] = []
        opened: list[Any] = []
        defer_release = False
        try:
            for name, database, workload in connections:
                connection = await database.acquire(workload)
                borrowed.append((database, connection, workload))
                kwargs[name] = connection
            if sessions:
                from .orm.session import Session

                by_key: dict[tuple[str, str], Any] = {}
                for name, key, registry, workload, tenant_marker in sessions:
                    session = by_key.get(key)
                    if session is None:
                        # Lazy: no connection is leased until the handler
                        # actually runs a statement. The tenant is *not* lazy:
                        # resolving it later would mean a session that exists
                        # without one, which is the state the whole binding
                        # exists to make unreachable.
                        context = None if tenant_marker is None else tenant_marker.resolve(request)
                        session = Session(registry, workload, tenant=context)
                        by_key[key] = session
                        opened.append(session)
                    kwargs[name] = session
            for key, use_cache, resolver in side_effect_resolvers:
                if use_cache and key in cache:
                    continue
                value = await resolver(request, cache, cleanups)
                if use_cache:
                    cache[key] = value
            for name, key, use_cache, resolver in resolvers:
                if use_cache and key in cache:
                    kwargs[name] = cache[key]
                else:
                    value = await resolver(request, cache, cleanups)
                    if use_cache:
                        cache[key] = value
                    kwargs[name] = value
            result = handler(request, **kwargs)
            if result.__class__ is _COROUTINE_TYPE:
                result = await result
            if borrowed or opened:
                from .response import StreamingResponse

                if isinstance(result, StreamingResponse):

                    async def release_after_stream() -> None:
                        # This internal cleanup belongs to the borrowed request
                        # resources, not to user background work. StreamingResponse
                        # runs it in a finally block so failed or cancelled emission
                        # still releases the connection. On success it finishes
                        # before _finish_http starts the user background callback.
                        await _release(borrowed, opened)

                    result._cleanup = release_after_stream
                    defer_release = True
            return result
        finally:
            if not defer_release:
                # Runs on success, exception, and cancellation alike, so a
                # connection is returned exactly once on every path.
                await _release(borrowed, opened)
            # Resume generator dependencies for cleanup, innermost first.
            for generator in reversed(cleanups):
                try:
                    await anext(generator)
                except StopAsyncIteration:
                    pass
                else:
                    await generator.aclose()

    if (
        compiled_path_plan
        and not query_specs
        and not header_specs
        and not cookie_specs
        and not needs_body
        and not needs_resources
    ):

        def bound_path(request: Request) -> Any:
            """Convert and call once; the application owns the one necessary await.

            ``activate_path_call`` returns the handler's result verbatim.  An
            async wrapper here used to await the user coroutine and return a
            second coroutine to the dispatcher, which then performed the same
            exact-type awaitability check around the wrapper.  Keeping this
            adapter synchronous lets the dispatcher await the user coroutine
            directly; synchronous endpoints remain synchronous too.
            """
            return _core.activate_path_call(handler, request, compiled_path_plan, ValidationError)

        # Startup wrappers use this predicate to decide whether their result
        # must be awaited before validation/negotiation.  Preserve the original
        # endpoint's convention even though this adapter deliberately returns
        # its coroutine instead of awaiting it itself.
        selected = (
            inspect.markcoroutinefunction(bound_path)
            if inspect.iscoroutinefunction(handler)
            else bound_path
        )
    else:
        selected = bound if needs_resources else bound_simple
    selected.__name__ = getattr(handler, "__name__", "bound")
    selected.__qualname__ = getattr(handler, "__qualname__", "bound")
    return selected


async def _release(borrowed: list, opened: list) -> None:
    """Close sessions and return connections, in reverse acquisition order.

    Every leg runs even when an earlier one fails, so one broken rollback
    cannot strand the remaining connections outside their pool. The first
    failure is re-raised once everything has been returned.
    """
    failure: BaseException | None = None
    for session in reversed(opened):
        try:
            await session.close()
        except Exception as error:  # noqa: BLE001 - re-raised below
            failure = failure or error
    for database, connection, workload in reversed(borrowed):
        try:
            await database.release(workload, connection)
        except Exception as error:  # noqa: BLE001 - re-raised below
            failure = failure or error
    if failure is not None:
        raise failure


__all__ = [
    "BindingSpec",
    "Body",
    "Cookie",
    "AppScope",
    "Depends",
    "Field",
    "File",
    "Form",
    "Header",
    "Path",
    "Query",
    "ResponseValidationError",
    "ValidationError",
    "compile_binder",
    "compile_message_negotiation",
    "compile_response_validator",
    "inspect_handler",
    "validate",
]
