"""Typed handler binding and validation, compiled at startup.

A handler that takes only ``request`` runs exactly as before — binding costs
nothing unless a signature asks for more. Extra parameters are bound by
inspecting the signature once at route-compile time (never per request):

- names matching a path placeholder become **path params**, converted to the
  annotated scalar type,
- a parameter annotated with a **dataclass** receives the validated JSON
  body,
- remaining scalar-annotated parameters come from the **query string**
  (defaults apply when absent).

Validation failures raise :class:`wreath.exceptions.UnprocessableEntity` with a
``detail`` listing each error's location, message, and type::

    @dataclass
    class NewItem:
        name: str
        price: float
        tags: list[str] = field(default_factory=list)

    @app.post("/items/{item_id}")
    async def create(request, item_id: int, item: NewItem, dry_run: bool = False):
        ...
"""

from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ._codecs import parse_qs
from ._json import loads as _json_loads
from ._native import _core
from .exceptions import BadRequest, UnprocessableEntity
from .request import Request

Handler = Callable[..., Awaitable[Any]]

_MISSING = dataclasses.MISSING
_NONE_TYPE = type(None)
_BINDING_SPEC_UNSET = object()

# Body-validation plan opcodes. These mirror the enum in
# src/wreath/_native/validate.c; the native validator executes a plan compiled
# here once per body type, and the pure `validate` remains the reference.
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


class _PlanUnsupported(Exception):
    """A body plan could not be compiled (e.g. a recursive dataclass); the
    caller falls back to the pure validator, which recurses lazily."""


def _compile_plan(annotation: Any, seen: frozenset[type]) -> tuple[Any, ...]:
    """Compile ``annotation`` into the native validator's plan tuples.

    Raises :class:`_PlanUnsupported` for shapes the flat plan cannot express
    (currently recursive dataclasses), signalling a pure-validator fallback.
    Semantics mirror :func:`_validate` exactly.
    """
    if annotation is Any or annotation is inspect.Parameter.empty:
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
            fields = tuple(
                (name, _compile_plan(field_annotation, child_seen), 1 if required else 0)
                for name, field_annotation, required in _dataclass_spec(annotation)
            )
            return (_OP_DATACLASS, annotation, fields)
        return (_OP_UNSUPPORTED, f"unsupported annotation {annotation!r}")
    if origin in (types.UnionType, typing.Union):
        options = typing.get_args(annotation)
        has_none = 1 if _NONE_TYPE in options else 0
        compiled = tuple(
            _compile_plan(option, seen) for option in options if option is not _NONE_TYPE
        )
        return (_OP_UNION, has_none, compiled, f"value matches no option of {annotation}")
    if origin is list or origin is tuple:
        args = typing.get_args(annotation)
        return (_OP_LIST, _compile_plan(args[0] if args else Any, seen))
    if origin is dict:
        args = typing.get_args(annotation)
        return (_OP_DICT, _compile_plan(args[1] if len(args) == 2 else Any, seen))
    return (_OP_UNSUPPORTED, f"unsupported annotation {annotation!r}")


class ValidationError(Exception):
    """Collected field errors; converted to a 422 at the route boundary."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(errors)} validation error(s)")
        self.errors = errors


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
    """Validate ``value`` (a decoded-JSON shape) against ``annotation``.

    Returns the validated (possibly coerced) value or raises ValidationError.
    Supported annotations: scalars (str/int/float/bool), Any, None, list[T],
    tuple[T, ...] (as list input), dict[str, T], Optional/unions, and
    dataclasses (recursively).
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


def _validate(annotation: Any, value: Any, loc: tuple[Any, ...], errors: list,
              budget: list) -> Any:
    if budget[0] <= 0:
        # Budget exhausted: mark it (negative sentinel) and stop descending.
        budget[0] = -1
        return value
    budget[0] -= 1
    if annotation is Any or annotation is inspect.Parameter.empty:
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
        if dataclasses.is_dataclass(annotation):
            return _validate_dataclass(annotation, value, loc, errors, budget)
        errors.append(
            _error(loc, f"unsupported annotation {annotation!r}", "unsupported")
        )
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
        errors.append(
            _error(loc, f"value matches no option of {annotation}", "union")
        )
        return value

    if origin is list or origin is tuple:
        if not isinstance(value, list):
            errors.append(_error(loc, "value is not an array", "list"))
            return value
        args = typing.get_args(annotation)
        item_type = args[0] if args else Any
        return [
            _validate(item_type, item, (*loc, index), errors, budget)
            for index, item in enumerate(value)
        ]

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


# Resolved field specs per dataclass: (name, annotation, required). Type-hint
# evaluation is expensive; it must happen once per class, never per request.
_DATACLASS_SPECS: dict[type, tuple[tuple[str, Any, bool], ...]] = {}


def _dataclass_spec(cls: type) -> tuple[tuple[str, Any, bool], ...]:
    spec = _DATACLASS_SPECS.get(cls)
    if spec is None:
        hints = typing.get_type_hints(cls)
        spec = tuple(
            (
                field.name,
                hints.get(field.name, Any),
                field.default is _MISSING and field.default_factory is _MISSING,
            )
            for field in dataclasses.fields(cls)
        )
        _DATACLASS_SPECS[cls] = spec
    return spec


def _validate_dataclass(cls: Any, value: Any, loc: tuple[Any, ...], errors: list,
                        budget: list) -> Any:
    if isinstance(value, cls):
        return value
    if not isinstance(value, dict):
        errors.append(_error(loc, "value is not an object", "dict"))
        return value
    kwargs: dict[str, Any] = {}
    spec = _dataclass_spec(cls)
    for name, annotation, required in spec:
        if name in value:
            kwargs[name] = _validate(annotation, value[name], (*loc, name), errors, budget)
        elif required:
            errors.append(_error((*loc, name), "field is required", "missing"))
    if len(value) > len(kwargs):
        known = {name for name, _, _ in spec}
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
            raise ValidationError(
                [_error(loc, f"{raw!r} is not an integer", "int")]
            ) from None
    if annotation is float:
        try:
            return float(raw)
        except ValueError:
            raise ValidationError(
                [_error(loc, f"{raw!r} is not a number", "float")]
            ) from None
    if annotation is bool:
        lowered = raw.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValidationError([_error(loc, f"{raw!r} is not a boolean", "bool")])
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
    """Clamp or reject a converted numeric ``value`` against a range.

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
    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Query:
    """Marks a query-string parameter, optionally with a reusable numeric range.

    ``minimum``/``maximum`` bound a valid ``int`` or ``float`` value. ``overflow``
    decides what an out-of-range value does: ``"error"`` (the default) returns the
    existing structured 422, ``"clamp"`` pins it to the nearest bound. Missing input
    still falls back to the handler default before any bound is considered, and
    invalid integer syntax remains an error regardless of ``overflow``.
    """

    alias: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    overflow: str = "error"

    def __post_init__(self) -> None:
        if self.overflow not in ("error", "clamp"):
            raise ValueError(
                f"Query overflow must be 'error' or 'clamp', got {self.overflow!r}"
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(
                f"Query minimum {self.minimum!r} exceeds maximum {self.maximum!r}"
            )


@dataclasses.dataclass(frozen=True, slots=True)
class Header:
    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Cookie:
    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Body:
    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Form:
    alias: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class File:
    alias: str | None = None


_SOURCE_MARKERS = (Path, Query, Header, Cookie, Body, Form, File)


class Depends:
    """Marks a handler parameter as provided by a dependency callable.

    The callable takes ``request`` (plus its own ``Depends`` parameters,
    resolved recursively) and may be a plain function, a coroutine function,
    or an async generator — generators yield the value and resume for cleanup
    after the handler finishes, even on error::

        async def get_session(request):
            session = Session()
            try:
                yield session
            finally:
                await session.close()

        @app.get("/items")
        async def items(request, session = Depends(get_session)):
            ...

    Within one request, each callable resolves at most once (``use_cache``).
    """

    __slots__ = ("fn", "use_cache")

    def __init__(self, fn: Callable[..., Any], *, use_cache: bool = True) -> None:
        self.fn = fn
        self.use_cache = use_cache


Resolver = Callable[[Request, dict, list], Awaitable[Any]]


def _compile_dependency(fn: Callable[..., Any], seen: tuple) -> Resolver:
    # `seen` is accepted for the existing call sites. Compilation memoizes a
    # resolver per callable and tracks the active path in a set, so a shared
    # dependency DAG compiles each callable once (not once per path, which is
    # exponential) and a depth-D chain does O(D) membership work (not O(D^2)).
    return _compile_dep(fn, {}, set(), [])


def _compile_dep(
    fn: Callable[..., Any],
    memo: dict[Callable[..., Any], Resolver],
    active: set[Callable[..., Any]],
    active_order: list[Callable[..., Any]],
) -> Resolver:
    cached = memo.get(fn)
    if cached is not None:
        return cached
    if fn in active:
        cycle = " -> ".join(
            repr(node) for node in (*active_order[active_order.index(fn) :], fn)
        )
        raise TypeError(f"circular dependency through {fn!r}: {cycle}")
    active.add(fn)
    active_order.append(fn)
    try:
        try:
            parameters = list(inspect.signature(fn).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        nested: list[tuple[str, Depends, Resolver]] = []
        for parameter in parameters[1:]:
            default = parameter.default
            if isinstance(default, Depends):
                nested.append(
                    (parameter.name, default, _compile_dep(default.fn, memo, active, active_order))
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

    async def resolve(request: Request, cache: dict, cleanups: list) -> Any:
        kwargs: dict[str, Any] = {}
        for name, marker, resolver in nested:
            if marker.use_cache and marker.fn in cache:
                kwargs[name] = cache[marker.fn]
            else:
                value = await resolver(request, cache, cleanups)
                if marker.use_cache:
                    cache[marker.fn] = value
                kwargs[name] = value
        if is_async_gen:
            generator = fn(request, **kwargs)
            value = await anext(generator)
            cleanups.append(generator)
            return value
        result = fn(request, **kwargs)
        if is_coroutine or inspect.isawaitable(result):
            return await result
        return result

    memo[fn] = resolve
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
                    raise ValidationError(
                        [_error(("form", alias), "field is required", "missing")]
                    )
                kwargs[name] = default
            else:
                kwargs[name] = _convert_scalar(annotation, raw, ("form", alias))
        for name, alias, _annotation, default in self._files:
            upload = parsed.files.get(alias)
            if upload is None:
                if default is inspect.Parameter.empty:
                    raise ValidationError(
                        [_error(("file", alias), "file is required", "missing")]
                    )
                kwargs[name] = default
            else:
                kwargs[name] = upload


def _unwrap_form_type(annotation: Any) -> Any:
    """Peel ``Mapped[T]`` and ``Optional[T]`` down to the scalar type that
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
    """``(field_name, python_type, required)`` for each scalar field of a
    dataclass or ORM model bound from multipart form fields. File parts are not
    model fields — bind an upload with a separate ``File()`` parameter."""
    try:
        hints = typing.get_type_hints(annotation)
    except (TypeError, ValueError):
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
    for field in dataclasses.fields(annotation):
        ptype = _unwrap_form_type(hints.get(field.name, field.type))
        required = (
            field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        )
        result.append((field.name, ptype, required))
    return tuple(result)


class _FormModelValidationTape:
    """Bind a whole dataclass/ORM model from multipart form fields, then run the
    SAME native body validator (the JSON-body path) over the assembled dict — so
    a form-posted model is validated exactly like a JSON-posted one. Reuses the
    native multipart parser (``request.form()``) and the native validation tape;
    no new native code. File parts are bound by separate ``File()`` params.

    TODO(pure-twin): the reuse means there is nothing new to twin — validation
    already runs native-or-pure via ``_body_validator``; this tape is pure-Python
    glue and works identically under ``WREATH_PURE=1``.
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


def _body_validator(annotation: Any) -> _BodyValidator:
    """Compile the body checker once, at route-compile time.

    A model validates a payload against its own columns in a single pass, so the
    values are proven once and land straight in the model's cells rather than
    being checked against a dataclass and then re-checked on assignment.
    """
    if _is_model(annotation):
        from .orm.validation import compile_model_validator

        return _BodyValidator(compile_model_validator(annotation))

    if _core is not None:
        # Compile the annotation into a flat plan once; the native validator
        # then checks a whole body in one call. Plans that cannot be flattened
        # (recursive dataclasses) fall through to the pure validator.
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

            def native_validate(payload: Any, loc: tuple[Any, ...]) -> Any:
                return checked(run_validation(plan, payload, loc))

            def native_decode(data: bytes, loc: tuple[Any, ...]) -> Any:
                return checked(decode_json(data, plan, loc))

            return _BodyValidator(native_validate, native_decode)

    def validate_annotation(payload: Any, loc: tuple[Any, ...]) -> Any:
        return validate(annotation, payload, loc)

    return _BodyValidator(validate_annotation)


def _path_placeholders(path: str) -> frozenset[str]:
    names = set()
    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}") and len(segment) > 2:
            names.add(segment[1:-1])
    return frozenset(names)


@dataclasses.dataclass(frozen=True, slots=True)
class BindingSpec:
    """The compiled shape of a typed handler signature."""

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


def inspect_handler(handler: Handler, path: str) -> BindingSpec | None:
    """The binding spec for ``handler`` on ``path``; None for request-only
    handlers (or objects whose signature cannot be inspected)."""
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return None
    parameters = tuple(signature.parameters.values())
    if len(parameters) <= 1:
        return None
    try:
        hints = typing.get_type_hints(handler, include_extras=True)
    except (TypeError, ValueError):
        return None

    placeholders = _path_placeholders(path)
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
        database_marker = next(
            (item for item in metadata if isinstance(item, FromDatabase)), None
        )
        orm_marker = next((item for item in metadata if isinstance(item, FromORM)), None)
        source = next(
            (item for item in metadata if isinstance(item, _SOURCE_MARKERS)), None
        )
        alias = parameter.name if source is None or source.alias is None else source.alias
        default = parameter.default
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
                    raise TypeError(
                        f"handler {label!s} declares two form-model parameters"
                    )
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
        raise TypeError(
            "a handler cannot combine a form-model with individual Form() fields"
        )
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
) -> Handler:
    """Wrap ``handler`` so typed parameters are bound per request.

    Handlers whose signature is exactly ``(request)`` are returned unchanged.
    """
    spec = (
        inspect_handler(handler, path)
        if binding_spec is _BINDING_SPEC_UNSET
        else typing.cast(BindingSpec | None, binding_spec)
    )
    if spec is None and not dependencies:
        return handler
    path_specs = () if spec is None else spec.path_params
    query_specs = () if spec is None else spec.query_params
    query_constraints = {} if spec is None else dict(spec.query_constraints)
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
    resolvers: tuple[tuple[str, Depends, Resolver], ...] = tuple(
        (name, marker, _compile_dependency(marker.fn, ()))
        for name, marker in (() if spec is None else spec.depends)
    )
    side_effect_resolvers = tuple(
        (marker, _compile_dependency(marker.fn, ())) for marker in dependencies
    )
    configured = databases or {}
    connections: list[tuple[str, Any, Any]] = []
    for name, marker in (() if spec is None else spec.connections):
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
    sessions: list[tuple[str, tuple[str, str], Any, Any]] = []
    if spec is not None and spec.sessions:
        from .orm.session import compile_session_binding

        for name, marker in spec.sessions:
            registry_name, registry = compile_session_binding(registries, marker)
            sessions.append((name, (registry_name, marker.workload), registry, marker.workload))

    async def bound(request: Request) -> Any:
        kwargs: dict[str, Any] = {}
        for name, alias, annotation in path_specs:
            kwargs[name] = _convert_scalar(
                annotation, request.path_params[alias], ("path", alias)
            )
        if query_specs:
            query = parse_qs(request.scope.get("query_string", b""))
            values: dict[str, str] = {}
            for key, value in query:
                values.setdefault(key, value)
            for name, alias, annotation, default in query_specs:
                raw = values.get(alias)
                if raw is None:
                    if default is inspect.Parameter.empty:
                        raise ValidationError(
                            [_error(("query", alias), "parameter is required", "missing")]
                        )
                    kwargs[name] = default
                else:
                    converted = _convert_scalar(annotation, raw, ("query", alias))
                    constraint = query_constraints.get(name)
                    if constraint is not None:
                        converted = _apply_constraint(
                            converted, constraint, ("query", alias)
                        )
                    kwargs[name] = converted
        for name, alias, annotation, default in header_specs:
            raw = request.header(alias)
            if raw is None:
                if default is inspect.Parameter.empty:
                    raise ValidationError(
                        [_error(("header", alias), "parameter is required", "missing")]
                    )
                kwargs[name] = default
            else:
                kwargs[name] = _convert_scalar(annotation, raw, ("header", alias))
        if cookie_specs:
            cookies = request.cookies
            for name, alias, annotation, default in cookie_specs:
                raw = cookies.get(alias)
                if raw is None:
                    if default is inspect.Parameter.empty:
                        raise ValidationError(
                            [_error(("cookie", alias), "parameter is required", "missing")]
                        )
                    kwargs[name] = default
                else:
                    kwargs[name] = _convert_scalar(annotation, raw, ("cookie", alias))
        if form_specs or file_specs:
            await form_tape.decode_multipart_validation_tape(request, kwargs)
        if form_model_tape is not None:
            await form_model_tape.decode(request, kwargs)
        if body_spec is not None and body_validator is not None:
            name, _annotation = body_spec
            try:
                body = await request.body()
                kwargs[name] = body_validator.decode_json_validation_tape(body, ("body",))
            except ValueError as exc:
                raise BadRequest(f"invalid JSON body: {exc}") from None
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
                for name, key, registry, workload in sessions:
                    session = by_key.get(key)
                    if session is None:
                        # Lazy: no connection is leased until the handler
                        # actually runs a statement.
                        session = Session(registry, workload)
                        by_key[key] = session
                        opened.append(session)
                    kwargs[name] = session
            for marker, resolver in side_effect_resolvers:
                if marker.use_cache and marker.fn in cache:
                    continue
                value = await resolver(request, cache, cleanups)
                if marker.use_cache:
                    cache[marker.fn] = value
            for name, marker, resolver in resolvers:
                if marker.use_cache and marker.fn in cache:
                    kwargs[name] = cache[marker.fn]
                else:
                    value = await resolver(request, cache, cleanups)
                    if marker.use_cache:
                        cache[marker.fn] = value
                    kwargs[name] = value
            result = await handler(request, **kwargs)
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

    bound.__name__ = getattr(handler, "__name__", "bound")
    bound.__qualname__ = getattr(handler, "__qualname__", "bound")
    return bound


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


def validation_error_response(error: ValidationError) -> UnprocessableEntity:
    return UnprocessableEntity(str(error))


__all__ = [
    "BindingSpec",
    "Body",
    "Cookie",
    "Depends",
    "File",
    "Form",
    "Header",
    "Path",
    "Query",
    "ValidationError",
    "compile_binder",
    "inspect_handler",
    "validate",
]
