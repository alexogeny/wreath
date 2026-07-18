"""Build the canonical typegen model from Wreath's routes and binding specs.

This is the single place Python annotations are interpreted for client
generation; OpenAPI consumes the same model. Nothing here emits target syntax
-- it produces the frozen records in :mod:`wreath.typegen.model`.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import types
import typing
from typing import Any

from ..binding import inspect_handler
from .model import (
    BOOLEAN,
    INTEGER,
    NULL,
    NUMBER,
    STRING,
    UNKNOWN,
    ApiModel,
    Diagnostic,
    Field,
    Model,
    Operation,
    Parameter,
    TypegenError,
    TypeRef,
)

_NONE_TYPE = type(None)
_SCALARS: dict[Any, TypeRef] = {
    bool: BOOLEAN,  # before int: bool is a subclass of int
    int: INTEGER,
    float: NUMBER,
    str: STRING,
}
_JS_KEYWORDS = frozenset(
    {
        "break", "case", "catch", "class", "const", "continue", "debugger",
        "default", "delete", "do", "else", "enum", "export", "extends", "false",
        "finally", "for", "function", "if", "import", "in", "instanceof", "new",
        "null", "return", "super", "switch", "this", "throw", "true", "try",
        "typeof", "var", "void", "while", "with", "let", "static", "yield",
        "await", "async",
    }
)


def _pascal(text: str) -> str:
    parts = [part for part in text.replace("-", "_").split("_") if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def derive_operation_id(method: str, path: str) -> str:
    """A deterministic camelCase id from method and path.

    ``GET /widgets/{widget_id}`` becomes ``getWidgetsByWidgetId`` -- stable
    across handler renames and reused handlers, unlike ``handler.__name__``.
    """
    words: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment.startswith("{") and segment.endswith("}"):
            words.append("By" + _pascal(segment[1:-1]))
        else:
            words.append(_pascal(segment))
    verb = method.lower()
    tail = "".join(words)
    if not tail:
        return verb + "Root"
    return verb + tail


def is_valid_identifier(name: str) -> bool:
    return name.isidentifier() and name not in _JS_KEYWORDS


def resolve_operation_ids(
    routes: list[Any],
) -> tuple[dict[tuple[int, str], str], tuple[Diagnostic, ...]]:
    """Map each (route index, method) to a unique operation id.

    Explicit ids are validated and preserved; missing ones are derived. A
    collision -- explicit or derived -- is a hard error naming both routes.
    """
    diagnostics: list[Diagnostic] = []
    resolved: dict[tuple[int, str], str] = {}
    owners: dict[str, tuple[str, str]] = {}
    for index, definition in enumerate(routes):
        explicit = definition.operation_id
        if explicit is not None and not is_valid_identifier(explicit):
            diagnostics.append(
                Diagnostic(
                    f"operation_id {explicit!r} is not a valid client identifier",
                    method=definition.methods[0] if definition.methods else None,
                    path=definition.path,
                )
            )
        for method in definition.methods:
            if explicit is not None:
                # Two methods on one route cannot share one explicit id.
                operation_id = (
                    explicit if len(definition.methods) == 1 else f"{explicit}{_pascal(method)}"
                )
            else:
                operation_id = derive_operation_id(method, definition.path)
            previous = owners.get(operation_id)
            current = (method, definition.path)
            if previous is not None:
                diagnostics.append(
                    Diagnostic(
                        f"duplicate operation id {operation_id!r} for "
                        f"{previous[0]} {previous[1]} and {method} {definition.path}",
                        method=method,
                        path=definition.path,
                    )
                )
            else:
                owners[operation_id] = current
            resolved[(index, method)] = operation_id
    return resolved, tuple(diagnostics)


class _ModelRegistry:
    """Claims a generated name before descending, so recursive types terminate
    and no two distinct Python types are merged by a shared ``__name__``."""

    def __init__(self) -> None:
        self._by_type: dict[int, str] = {}
        self._models: dict[str, Model | None] = {}
        self._owner: dict[str, Any] = {}

    def _claim_name(self, tp: Any) -> str:
        base = _pascal(tp.__name__)
        if base not in self._owner:
            return base
        # Deterministic qualified alias; module path disambiguates same-name
        # classes from different modules without merging them.
        module = getattr(tp, "__module__", "") or ""
        alias = f"{base}_{module.replace('.', '_')}"
        if alias not in self._owner:
            return alias
        qualified = f"{alias}_{_pascal(getattr(tp, '__qualname__', tp.__name__))}"
        if qualified not in self._owner:
            return qualified
        raise KeyError(base)

    def reference(self, tp: Any, build_fields: Any) -> str:
        key = id(tp)
        existing = self._by_type.get(key)
        if existing is not None:
            return existing
        try:
            name = self._claim_name(tp)
        except KeyError:
            raise
        self._by_type[key] = name
        self._owner[name] = tp
        self._models[name] = None  # claimed; recursion into fields is now safe
        self._models[name] = Model(name, tuple(build_fields()))
        return name

    def models(self) -> tuple[Model, ...]:
        return tuple(
            model for _name, model in sorted(self._models.items()) if model is not None
        )


class _Builder:
    def __init__(self, allow_unknown: bool) -> None:
        self.registry = _ModelRegistry()
        self.diagnostics: list[Diagnostic] = []
        self.allow_unknown = allow_unknown
        self._context: Diagnostic = Diagnostic("")

    def _unsupported(self, annotation: Any) -> TypeRef:
        self.diagnostics.append(
            dataclasses.replace(
                self._context,
                message=f"unsupported annotation {annotation!r}",
                annotation=_annotation_name(annotation),
            )
        )
        return UNKNOWN

    def type_ref(self, annotation: Any) -> TypeRef:
        if annotation is Any or annotation is inspect.Parameter.empty:
            return UNKNOWN
        if annotation is None or annotation is _NONE_TYPE:
            return NULL
        scalar = _SCALARS.get(annotation)
        if scalar is not None:
            return scalar
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            return self._enum_ref(annotation)
        origin = typing.get_origin(annotation)
        if origin is typing.Literal:
            return self._literal_ref(typing.get_args(annotation))
        if origin in (types.UnionType, typing.Union):
            return self._union_ref(typing.get_args(annotation))
        if origin is list or origin is set or origin is frozenset:
            args = typing.get_args(annotation)
            return TypeRef("array", arguments=(self.type_ref(args[0]) if args else UNKNOWN,))
        if origin is tuple:
            return self._tuple_ref(typing.get_args(annotation))
        if origin is dict:
            args = typing.get_args(annotation)
            if args and args[0] not in (str, Any):
                return self._unsupported(annotation)
            value = self.type_ref(args[1]) if len(args) == 2 else UNKNOWN
            return TypeRef("record", arguments=(value,))
        if dataclasses.is_dataclass(annotation) or _is_wreath_model(annotation):
            return self._reference(annotation)
        return self._unsupported(annotation)

    def _enum_ref(self, annotation: type[enum.Enum]) -> TypeRef:
        values: list[Any] = []
        for member in annotation:
            if isinstance(member.value, (str, int, float, bool)) or member.value is None:
                values.append(member.value)
            else:
                return self._unsupported(annotation)
        return TypeRef("literal", literals=tuple(values))

    def _literal_ref(self, args: tuple[Any, ...]) -> TypeRef:
        values: list[Any] = []
        for arg in args:
            if isinstance(arg, enum.Enum):
                arg = arg.value
            if isinstance(arg, (str, int, float, bool)) or arg is None:
                values.append(arg)
            else:
                return self._unsupported(arg)
        return TypeRef("literal", literals=tuple(values))

    def _union_ref(self, args: tuple[Any, ...]) -> TypeRef:
        members = tuple(self.type_ref(arg) for arg in args)
        # Deduplicate structurally while preserving declaration order.
        seen: set[TypeRef] = set()
        unique: list[TypeRef] = []
        for member in members:
            if member not in seen:
                seen.add(member)
                unique.append(member)
        if len(unique) == 1:
            return unique[0]
        return TypeRef("union", arguments=tuple(unique))

    def _tuple_ref(self, args: tuple[Any, ...]) -> TypeRef:
        if len(args) == 2 and args[1] is Ellipsis:
            return TypeRef("array", arguments=(self.type_ref(args[0]),))
        if not args:
            return TypeRef("array", arguments=(UNKNOWN,))
        return TypeRef("tuple", arguments=tuple(self.type_ref(arg) for arg in args))

    def _reference(self, annotation: Any) -> TypeRef:
        def build_fields() -> list[Field]:
            return list(self._model_fields(annotation))

        try:
            name = self.registry.reference(annotation, build_fields)
        except KeyError:
            return self._unsupported(annotation)
        return TypeRef("reference", name=name)

    def _model_fields(self, annotation: Any) -> list[Field]:
        if dataclasses.is_dataclass(annotation):
            try:
                hints = typing.get_type_hints(annotation, include_extras=False)
            except (TypeError, ValueError):
                hints = {}
            fields: list[Field] = []
            for dc_field in dataclasses.fields(annotation):
                required = (
                    dc_field.default is dataclasses.MISSING
                    and dc_field.default_factory is dataclasses.MISSING
                )
                fields.append(
                    Field(
                        dc_field.name,
                        self.type_ref(hints.get(dc_field.name, Any)),
                        required,
                    )
                )
            return fields
        return self._wreath_model_fields(annotation)

    def _wreath_model_fields(self, annotation: Any) -> list[Field]:
        columns = getattr(annotation, "__wreath_columns__", None)
        if columns is None:
            return []
        fields = []
        for column in columns:
            annotation_type = getattr(column, "type", Any)
            required = not getattr(column, "nullable", False) and getattr(
                column, "default", dataclasses.MISSING
            ) is dataclasses.MISSING
            fields.append(Field(column.name, self.type_ref(annotation_type), required))
        return fields


def _annotation_name(annotation: Any) -> str:
    return getattr(annotation, "__qualname__", None) or getattr(
        annotation, "__name__", None
    ) or repr(annotation)


def _is_wreath_model(annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    try:
        from ..orm.model import Model as OrmModel
    except ImportError:
        return False
    if not issubclass(annotation, OrmModel):
        return False
    return getattr(annotation, "__wreath_table__", None) is not None


def build_api_model(
    app: Any,
    *,
    title: str = "Wreath",
    version: str = "0.1.0",
    allow_unknown: bool = False,
) -> ApiModel:
    """Construct the canonical model for ``app``'s routes.

    Raises :class:`TypegenError` when strict (``allow_unknown=False``) and any
    annotation is unsupported, or on any operation-id collision regardless of
    strictness.
    """
    routes = list(app._routes)
    resolved_ids, id_diagnostics = resolve_operation_ids(routes)
    builder = _Builder(allow_unknown)
    operations: list[Operation] = []

    for index, definition in enumerate(routes):
        spec = inspect_handler(definition.endpoint, definition.path)
        # inspect_handler returns None for request-only handlers, so the return
        # annotation is resolved independently here -- a param-less handler still
        # has a typed response worth generating.
        return_annotation = _return_annotation(definition.endpoint)
        doc = inspect.getdoc(definition.endpoint)
        for method in definition.methods:
            operation_id = resolved_ids[(index, method)]
            builder._context = Diagnostic(
                "", operation_id=operation_id, method=method, path=definition.path
            )
            parameters, request_body, request_media, response = _operation_shape(
                builder, spec, definition, method, return_annotation
            )
            operations.append(
                Operation(
                    id=operation_id,
                    method=method,
                    path=definition.path,
                    parameters=tuple(parameters),
                    request_body=request_body,
                    request_body_media_type=request_media,
                    response_body=response,
                    tags=tuple(definition.tags),
                    summary=definition.summary,
                    description=doc or None,
                )
            )

    # Operation-id collisions are always fatal; unsupported annotations are
    # fatal only under strict generation.
    fatal = id_diagnostics + (
        () if allow_unknown else tuple(builder.diagnostics)
    )
    if fatal:
        raise TypegenError(fatal)

    operations.sort(key=lambda operation: operation.id)
    return ApiModel(
        title=title,
        version=version,
        models=builder.registry.models(),
        operations=tuple(operations),
    )


def _return_annotation(handler: Any) -> Any:
    try:
        hints = typing.get_type_hints(handler, include_extras=False)
    except (TypeError, ValueError, NameError):
        return inspect.Parameter.empty
    return hints.get("return", inspect.Parameter.empty)


def _operation_shape(
    builder: _Builder, spec: Any, definition: Any, method: str, return_annotation: Any
) -> tuple[list[Parameter], TypeRef | None, str | None, TypeRef]:
    parameters: list[Parameter] = []
    request_body: TypeRef | None = None
    request_media: str | None = None
    if spec is None:
        for segment in definition.path.split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                parameters.append(
                    Parameter(segment[1:-1], segment[1:-1], "path", STRING, True)
                )
        return parameters, None, None, builder.type_ref(return_annotation)

    for python_name, alias, annotation in spec.path_params:
        parameters.append(
            Parameter(python_name, alias, "path", builder.type_ref(annotation), True)
        )
    for location, bindings in (
        ("query", spec.query_params),
        ("header", spec.header_params),
        ("cookie", spec.cookie_params),
    ):
        for python_name, alias, annotation, default in bindings:
            parameters.append(
                Parameter(
                    python_name,
                    alias,
                    location,  # type: ignore[arg-type]
                    builder.type_ref(annotation),
                    default is inspect.Parameter.empty,
                )
            )
    if spec.body is not None:
        request_body = builder.type_ref(spec.body[1])
        request_media = "application/json"
    elif spec.form_params or spec.file_params:
        # Multipart bodies are sent as FormData; the first target types them as
        # a string-keyed record rather than inventing an anonymous model name.
        request_body = TypeRef("record", arguments=(UNKNOWN,))
        request_media = "multipart/form-data"
    response = builder.type_ref(return_annotation)
    return parameters, request_body, request_media, response


__all__ = [
    "build_api_model",
    "derive_operation_id",
    "is_valid_identifier",
    "resolve_operation_ids",
]
