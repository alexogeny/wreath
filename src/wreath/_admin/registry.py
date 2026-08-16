"""Registration and the generated views. Use `wreath.admin`."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal as _Decimal
from typing import Any, cast
from urllib.parse import quote, urlencode

from .._codecs import parse_qs
from ..audit_log import REDACTED, actor
from ..crud import Access, retrieval_fields, sensitive_fields
from ..crud import _apply_requirement as _apply_access
from ..pagination import MAX_SIZE, PageParams, paginate, sortable_fields
from ..response import HTMLResponse, RedirectResponse, Response
from ..temporal import Instant as _Instant
from .fields import FieldAccess, resolve_readable, resolve_writable
from .pages import (
    CONFIRM_TEMPLATE,
    CONTENT_SECURITY_POLICY,
    DETAIL_TEMPLATE,
    FORM_TEMPLATE,
    INDEX_TEMPLATE,
    LIST_TEMPLATE,
)

__all__ = ["Admin", "ModelAdmin"]

#: The operation vocabulary, taken from `wreath.crud` rather than invented
#: again: an admin's list/retrieve/create/update/delete are the same five
#: operations its CRUD routes have, so an `Access` mapping written for one
#: reads correctly against the other.
_OPERATIONS = ("list", "retrieve", "create", "update", "delete")
_READ_OPERATIONS = ("list", "retrieve")

#: How a column's declared PostgreSQL type becomes a form control. This decides
#: only the *affordance* the browser offers -- a spinner, a date picker -- and
#: an unlisted type falls back to a text input, which costs the affordance and
#: nothing else. What the submitted string becomes is `_FORM_TYPES` below, and
#: what is finally accepted is the column's own validation; neither depends on
#: this map, so a missing entry cannot widen what a form may store.
_INPUT_TYPES = {
    "int2": "number", "int4": "number", "int8": "number",
    "smallint": "number", "integer": "number", "bigint": "number",
    "float4": "number", "float8": "number", "numeric": "number",
    "date": "date", "timestamptz": "datetime-local", "timestamp": "datetime-local",
    "time": "time", "timetz": "time",
}
#: What a submitted string becomes for each declared column type. A form
#: transports text and the ORM stores typed values, and the ORM does **not**
#: parse: it refuses `'4'` for an `int8` and a `float` for a `numeric`. So the
#: conversion happens here, against the column's own declared type, and every
#: entry is one `wreath.binding` already knows how to parse -- except `numeric`,
#: which lands on `Decimal` because a float cannot hold one exactly.
#: A type absent from this map keeps its string: `text`, `varchar`, `uuid`,
#: `bytea` and the JSON types all accept one.
_FORM_TYPES: dict[str, Any] = {
    "int2": int, "int4": int, "int8": int,
    "smallint": int, "integer": int, "bigint": int,
    "float4": float, "float8": float,
    "numeric": _Decimal,
    "date": _date,
    "timestamptz": _Instant,
}

_NUMERIC_TYPES = frozenset({"float4", "float8", "numeric"})
#: Types that get a `<textarea>`. Deliberately **not** `text`: that is wreath's
#: ordinary string type, carrying names and email addresses far more often than
#: prose, and a textarea for every one of them is the wrong control almost
#: everywhere it would appear.
_MULTILINE_TYPES = frozenset({"json", "jsonb", "xml"})
_BOOLEAN_TYPES = frozenset({"bool", "boolean"})

#: What a withheld value renders as. `wreath.audit_log`'s marker rather than a
#: second spelling of the same idea: "this field exists and you may not see it"
#: is one fact, and the admin and the audit trail should not disagree about how
#: it is written down.
WITHHELD_MARKER = REDACTED


class AdminError(Exception):
    """A misdeclared admin, raised at registration or at `router()`."""


def _label(name: str) -> str:
    """`taken_at` -> `Taken at`. The column name, made readable, nothing more."""
    return name.replace("_", " ").capitalize()


def _display(value: Any) -> str:
    """One column value as display text. Escaping is the template's job."""
    if value is None:
        return "—"
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def _form_value(value: Any) -> str:
    """One column value as a form control's `value`. Empty for absent."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


@dataclass(frozen=True, slots=True)
class ModelAdmin:
    """One registered model and how the admin presents it."""

    model: type
    slug: str
    label: str
    table: str
    primary_key: str
    #: Columns that may leave the server at all, in declared order. Sensitive
    #: and retrieval columns are already gone; see `Admin.register`.
    columns: tuple[str, ...]
    #: The subset shown in the list view.
    list_columns: tuple[str, ...]
    #: The subset a form may set, before per-field policy narrows it further.
    editable: tuple[str, ...]
    sortable: frozenset[str]
    #: Each editable column's declared Python type, resolved once at
    #: registration. A form transports strings; this is what they become.
    python_types: Mapping[str, Any]
    field_access: Mapping[str, FieldAccess]
    operations: frozenset[str]
    page_size: int
    #: The Cedar resource per-field rules are asked about. Resource-type level,
    #: matching the permission manifest's own split and `crud`'s `precision`.
    resource: str
    meta: Mapping[str, Any] = field(default_factory=dict)


class Admin:
    """A generated administration surface over registered models.

    ```python
    admin = Admin(
        open_session,
        authorize={"read": Access.roles("staff"),
                   "write": Access.roles("staff").within(300)},
        csrf=verify_admin_form,
    )
    admin.register(Photo, list_columns=("taken_at", "species"))
    admin.register(User, field_access={"email": FieldAccess(read="read_contact")})
    app.include_router(admin.router("/admin"))
    ```

    **Opt-in three times**, because an admin concentrates read access to
    everything into one authenticated surface and an admin mounted by accident
    is worse than a CRUD route mounted by accident: an explicit `authorize`
    rule (there is no default and `Access.public()` is refused), an explicit
    `register` per model, and an explicit `include_router`.

    Args:
        open_session: `(request) -> Session`, one per request, closed by the view
        authorize: one `wreath.crud.Access` rule, or a mapping keyed by
            operation (`list`/`retrieve`/`create`/`update`/`delete`), group
            (`read`/`write`) or `"*"`. The same vocabulary generated CRUD uses.
        csrf: `(request) -> bool`, optionally async, proving a mutating request
            came from this admin's own form. Required before any write
            operation is generated -- see `router`.
        title: the heading and `<title>` of the overview page

    Raises:
        AdminError: `authorize` admits anonymous callers.
    """

    __slots__ = ("_csrf", "_open_session", "_registered", "_rules", "_title")

    def __init__(
        self,
        open_session: Callable[[Any], Any],
        *,
        authorize: Access | Mapping[str, Access],
        csrf: Callable[[Any], Any] | None = None,
        title: str = "Administration",
    ) -> None:
        self._open_session = open_session
        self._csrf = csrf
        self._title = title
        self._registered: dict[str, ModelAdmin] = {}
        rules = {op: _rule_for(authorize, op) for op in _OPERATIONS}
        public = sorted(op for op, rule in rules.items() if rule.kind == "public")
        if public:
            raise AdminError(
                "an admin route may not be public; "
                f"{', '.join(public)} resolved to Access.public(). An admin "
                "concentrates read access to every registered model into one "
                "surface, so an anonymous caller is never the intent. Build the "
                "rule from authenticated(), roles(), permissions() or cedar()."
            )
        self._rules = rules

    @property
    def models(self) -> tuple[ModelAdmin, ...]:
        """Every registered model, in registration order."""
        return tuple(self._registered.values())

    def register(
        self,
        model: type,
        *,
        slug: str | None = None,
        label: str | None = None,
        list_columns: Iterable[str] | None = None,
        field_access: Mapping[str, FieldAccess] | None = None,
        operations: Iterable[str] = _OPERATIONS,
        readonly: Iterable[str] = (),
        exclude: Iterable[str] = (),
        expose: Iterable[str] = (),
        page_size: int = 25,
    ) -> ModelAdmin:
        """Expose `model` in the admin. Nothing is exposed by existing.

        Withholding follows `wreath.crud` exactly rather than being decided
        again here: a column whose *name* looks like a secret
        (`crud.sensitive_fields`) and a retrieval index such as a `Vector` or
        `TsVector` (`crud.retrieval_fields`) are absent from every view and from
        every form unless named in `expose`. A generated column is readable and
        never writable. Two layers deciding this separately is how they drift.

        Args:
            slug: the URL segment; the lower-cased class name by default
            label: the human name; the class name by default
            list_columns: the columns the list view shows, in order. Defaults to
                every readable column, which is right for a narrow model and
                worth narrowing for a wide one.
            field_access: per-column Cedar rules -- see `FieldAccess`
            operations: which of list/retrieve/create/update/delete to generate
            readonly: columns a form never sets (server-set ones)
            exclude: columns no view ever shows
            expose: columns withheld by default to show anyway, an explicit act

        Raises:
            AdminError: the model has a composite primary key, a name collides
                with a registration already made, or a column named in
                `list_columns`, `field_access`, `readonly` or `expose` is not a
                column of `model` -- each of which would otherwise read as
                protection or presentation that silently does nothing.
        """
        spec = cast("Any", model)
        columns = spec.__wreath_column_map__
        keys = spec.__wreath_primary_key__
        if len(keys) != 1:
            raise AdminError(
                f"{model.__name__} has a composite primary key; the admin "
                "addresses a row by a single path segment"
            )
        primary_key = keys[0].python_name

        unknown_ops = sorted(set(operations) - set(_OPERATIONS))
        if unknown_ops:
            raise AdminError(
                f"unknown operation(s) {', '.join(unknown_ops)}; "
                f"expected some of {', '.join(_OPERATIONS)}"
            )

        exposed = frozenset(expose)
        excluded = frozenset(exclude)
        readonly_set = frozenset(readonly)
        access = dict(field_access or {})
        for role, names in (
            ("expose", exposed), ("exclude", excluded), ("readonly", readonly_set),
            ("field_access", frozenset(access)),
        ):
            missing = sorted(name for name in names if name not in columns)
            if missing:
                raise AdminError(
                    f"{model.__name__} has no column(s) {', '.join(missing)}; "
                    f"`{role}` names them"
                )

        sensitive = sensitive_fields(model)
        withheld = sensitive | retrieval_fields(model)
        shown = tuple(
            name for name in columns
            if name not in excluded and (name not in withheld or name in exposed)
        )
        shown_names = frozenset(shown)
        generated = frozenset(name for name, item in columns.items() if item.generated)
        editable = tuple(
            name for name in shown
            if name != primary_key
            and name not in readonly_set
            and name not in generated
            and name not in sensitive
        )

        if list_columns is None:
            listed = shown
        else:
            listed = tuple(list_columns)
            missing = sorted(name for name in listed if name not in shown_names)
            if missing:
                raise AdminError(
                    f"`list_columns` names {', '.join(missing)}, which this "
                    f"registration does not show. A column excluded, or withheld "
                    f"as sensitive or as a retrieval index, is not listable; "
                    f"name it in `expose=` if it should be."
                )

        resolved_slug = slug or model.__name__.lower()
        if resolved_slug in self._registered:
            raise AdminError(
                f"slug {resolved_slug!r} is registered to "
                f"{self._registered[resolved_slug].model.__name__}; pass slug= "
                "to distinguish them"
            )
        entry = ModelAdmin(
            model=model,
            slug=resolved_slug,
            label=label or model.__name__,
            table=getattr(spec, "__wreath_table__", resolved_slug),
            primary_key=primary_key,
            columns=shown,
            list_columns=listed,
            editable=editable,
            sortable=frozenset(sortable_fields(model)) & shown_names,
            python_types=_python_types(model, editable),
            field_access=access,
            operations=frozenset(operations),
            page_size=max(1, min(MAX_SIZE, page_size)),
            resource=f'{model.__name__}::"*"',
        )
        self._registered[resolved_slug] = entry
        return entry

    def router(self, prefix: str = "/admin") -> Any:
        """Build the `Router` for every registered model.

        Route paths are literal per model, so `/{slug}/new` is a static segment
        that wins over `/{slug}/{pk}`. The consequence is worth knowing: a model
        with a *text* primary key whose value is exactly `new` is not reachable
        through the admin. Every generated admin makes this trade; it is written
        down here rather than discovered.

        Raises:
            AdminError: nothing is registered, or a write operation is
                registered with no `csrf` verifier. A server-rendered form
                cannot carry `CsrfPolicy`'s header, so an admin that
                accepted posts without one would be a cross-site write against
                the most privileged surface in the application.
        """
        from ..router import Router

        if not self._registered:
            raise AdminError(
                "no models are registered; call admin.register(Model) before "
                "admin.router(). An admin with no registrations is a mount that "
                "exposes nothing and reads as if it exposed everything."
            )
        writing = sorted(
            {op for entry in self._registered.values() for op in entry.operations}
            - set(_READ_OPERATIONS)
        )
        if writing and self._csrf is None:
            raise AdminError(
                f"the admin generates {', '.join(writing)} but was given no "
                "`csrf` verifier. wreath.policy.CsrfPolicy reads its "
                "token from a request header, which a plain HTML form post "
                "cannot carry, so it cannot protect these routes -- mounting it "
                "would refuse every admin form instead. Pass csrf=(request) -> "
                "bool, or register operations=('list', 'retrieve') for a "
                "read-only admin."
            )

        base = prefix.rstrip("/")
        router = Router(prefix=base, tags=("admin",))
        entries = tuple(self._registered.values())
        nav = tuple(
            {"label": entry.label, "url": f"{base}/{entry.slug}/", "table": entry.table}
            for entry in entries
        )
        # `Router(prefix="/admin")` mounts `"/"` as `/admin`, not `/admin/`, so
        # the overview link is the bare prefix. A `base` of `""` still needs a
        # path, which is where the fallback goes.
        shell = {"models": nav, "home_url": base or "/"}

        index_rule = self._rules["retrieve"]

        @router.get("/")
        async def index(request: Any) -> Response:
            return _html(INDEX_TEMPLATE, {
                **shell,
                "title": self._title,
                "heading": self._title,
                "intro": (
                    "Every model registered on this admin. This surface is for "
                    "operators, not for customers."
                ),
                "empty": not nav,
            })

        _apply_access(index, index_rule)

        for entry in entries:
            self._mount(router, base, shell, entry)
        return router

    def _mount(
        self, router: Any, base: str, shell: dict[str, Any], entry: ModelAdmin
    ) -> None:
        """Register one model's views. One closure set per registration."""
        open_session = self._open_session
        rules = self._rules
        csrf = self._csrf
        root = f"{base}/{entry.slug}"
        admin_id = id(entry)

        async def readable(request: Any) -> frozenset[str]:
            return await resolve_readable(
                request, _authorizer(request), entry.field_access,
                entry.columns, entry.resource, admin_id,
            )

        async def writable(request: Any) -> frozenset[str]:
            return await resolve_writable(
                request, _authorizer(request), entry.field_access,
                entry.editable, entry.resource, admin_id,
            )

        def cells(instance: Any, names: tuple[str, ...], allowed: frozenset[str]) -> list[dict]:
            """The one path a column value takes into a render context.

            A name outside `allowed` is never read off the instance: the
            withheld marker is a constant, so the value does not reach the
            template even as something the template chooses not to draw.
            """
            return [
                {"label": _label(name), "value": WITHHELD_MARKER, "withheld": True}
                if name not in allowed
                else {"label": _label(name), "value": _display(getattr(instance, name, None)),
                      "withheld": False}
                for name in names
            ]

        if "list" in entry.operations:

            @router.get(f"/{entry.slug}/")
            async def list_(request: Any) -> Response:
                allowed = await readable(request)
                session = open_session(request)
                try:
                    params = _page_params(request, entry)
                    page = await paginate(
                        session, cast("Any", entry.model).select(), params,
                        allow_sort=entry.sortable,
                    )
                finally:
                    await session.close()
                query = _query_of(request)
                return _html(LIST_TEMPLATE, {
                    **shell,
                    "title": f"{entry.label} — {self._title}",
                    "heading": entry.label,
                    "model_label": entry.label,
                    "caption": f"{entry.label} rows {page.total} in total",
                    "headers": [
                        {"label": _label(name),
                         "sort_url": _sort_url(root, query, name, entry)}
                        for name in entry.list_columns
                    ],
                    "rows": [
                        {"url": f"{root}/{quote(str(getattr(row, entry.primary_key)), safe='')}",
                         "label": f"{entry.label} {getattr(row, entry.primary_key)}",
                         "cells": cells(row, entry.list_columns, allowed)}
                        for row in page.items
                    ],
                    "empty": not page.items,
                    "can_create": "create" in entry.operations,
                    "create_url": f"{root}/new",
                    "page": {
                        "has_prev": page.has_prev,
                        "has_next": page.has_next,
                        "prev_url": _page_url(root, query, page.page - 1),
                        "next_url": _page_url(root, query, page.page + 1),
                        "summary": f"Page {page.page} of {page.pages}, {page.total} rows",
                    },
                })

            _apply_access(list_, rules["list"])

        if "retrieve" in entry.operations:

            @router.get(f"/{entry.slug}/{{pk}}")
            async def retrieve(request: Any) -> Response:
                allowed = await readable(request)
                session = open_session(request)
                try:
                    instance = await _load(session, entry, request)
                finally:
                    await session.close()
                if instance is None:
                    return _missing(entry, root, shell, self._title)
                key = getattr(instance, entry.primary_key)
                return _html(DETAIL_TEMPLATE, {
                    **shell,
                    "title": f"{entry.label} {key} — {self._title}",
                    "heading": f"{entry.label} {key}",
                    "model_label": entry.label,
                    "fields": cells(instance, entry.columns, allowed),
                    "list_url": f"{root}/",
                    "can_edit": "update" in entry.operations,
                    "edit_url": f"{root}/{quote(str(key), safe='')}/edit",
                    "can_delete": "delete" in entry.operations,
                    "delete_url": f"{root}/{quote(str(key), safe='')}/delete",
                })

            _apply_access(retrieve, rules["retrieve"])

        if "create" in entry.operations:

            @router.get(f"/{entry.slug}/new")
            async def create_form(request: Any) -> Response:
                allowed = await writable(request)
                return _html(FORM_TEMPLATE, {
                    **shell,
                    "title": f"New {entry.label} — {self._title}",
                    "heading": f"New {entry.label}",
                    "action": f"{root}/new",
                    "fields": _form_fields(entry, None, allowed),
                    "empty": not allowed,
                    "submit_label": f"Create {entry.label}",
                    "cancel_url": f"{root}/",
                    "errors": (),
                    "has_errors": False,
                })

            @router.post(f"/{entry.slug}/new")
            async def create(request: Any) -> Response:
                refusal = await _csrf_refusal(csrf, request)
                if refusal is not None:
                    return refusal
                allowed = await writable(request)
                submitted = await _submitted(request, entry, allowed)
                problems = submitted.problems
                if not problems:
                    session = open_session(request)
                    try:
                        instance = cast("Any", entry.model)(**submitted.values)
                        with actor(_actor_name(request)):
                            async with session.begin():
                                session.add(instance)
                                await session.flush()
                            key = getattr(instance, entry.primary_key)
                        return RedirectResponse(
                            f"{root}/{quote(str(key), safe='')}", status=303
                        )
                    except (TypeError, ValueError) as error:
                        problems = [{"message": str(error)}]
                    finally:
                        await session.close()
                return _html(FORM_TEMPLATE, {
                    **shell,
                    "title": f"New {entry.label} — {self._title}",
                    "heading": f"New {entry.label}",
                    "action": f"{root}/new",
                    "fields": _form_fields(entry, None, allowed, submitted.raw),
                    "empty": not allowed,
                    "submit_label": f"Create {entry.label}",
                    "cancel_url": f"{root}/",
                    "errors": problems,
                    "has_errors": True,
                }, status=422)

            _apply_access(create_form, rules["create"])
            _apply_access(create, rules["create"])

        if "update" in entry.operations:

            @router.get(f"/{entry.slug}/{{pk}}/edit")
            async def edit_form(request: Any) -> Response:
                allowed = await writable(request)
                session = open_session(request)
                try:
                    instance = await _load(session, entry, request)
                finally:
                    await session.close()
                if instance is None:
                    return _missing(entry, root, shell, self._title)
                key = getattr(instance, entry.primary_key)
                return _html(FORM_TEMPLATE, {
                    **shell,
                    "title": f"Edit {entry.label} {key} — {self._title}",
                    "heading": f"Edit {entry.label} {key}",
                    "action": f"{root}/{quote(str(key), safe='')}/edit",
                    "fields": _form_fields(entry, instance, allowed),
                    "empty": not allowed,
                    "submit_label": "Save changes",
                    "cancel_url": f"{root}/{quote(str(key), safe='')}",
                    "errors": (),
                    "has_errors": False,
                })

            @router.post(f"/{entry.slug}/{{pk}}/edit")
            async def update(request: Any) -> Response:
                refusal = await _csrf_refusal(csrf, request)
                if refusal is not None:
                    return refusal
                allowed = await writable(request)
                submitted = await _submitted(request, entry, allowed)
                problems = submitted.problems
                session = open_session(request)
                try:
                    instance = await _load(session, entry, request)
                    if instance is None:
                        return _missing(entry, root, shell, self._title)
                    key = getattr(instance, entry.primary_key)
                    if not problems:
                        try:
                            with actor(_actor_name(request)):
                                async with session.begin():
                                    for name, value in submitted.values.items():
                                        setattr(instance, name, value)
                                    await session.flush()
                            return RedirectResponse(
                                f"{root}/{quote(str(key), safe='')}", status=303
                            )
                        except (TypeError, ValueError) as error:
                            problems = [{"message": str(error)}]
                    return _html(FORM_TEMPLATE, {
                        **shell,
                        "title": f"Edit {entry.label} {key} — {self._title}",
                        "heading": f"Edit {entry.label} {key}",
                        "action": f"{root}/{quote(str(key), safe='')}/edit",
                        "fields": _form_fields(entry, instance, allowed, submitted.raw),
                        "empty": not allowed,
                        "submit_label": "Save changes",
                        "cancel_url": f"{root}/{quote(str(key), safe='')}",
                        "errors": problems,
                        "has_errors": True,
                    }, status=422)
                finally:
                    await session.close()

            _apply_access(edit_form, rules["update"])
            _apply_access(update, rules["update"])

        if "delete" in entry.operations:

            @router.get(f"/{entry.slug}/{{pk}}/delete")
            async def delete_confirm(request: Any) -> Response:
                allowed = await readable(request)
                session = open_session(request)
                try:
                    instance = await _load(session, entry, request)
                finally:
                    await session.close()
                if instance is None:
                    return _missing(entry, root, shell, self._title)
                key = getattr(instance, entry.primary_key)
                return _html(CONFIRM_TEMPLATE, {
                    **shell,
                    "title": f"Delete {entry.label} {key} — {self._title}",
                    "heading": f"Delete {entry.label} {key}",
                    "prompt": (
                        f"This permanently deletes {entry.label} {key}. "
                        "The audit trail keeps the record of the deletion."
                    ),
                    "fields": cells(instance, entry.columns, allowed),
                    "action": f"{root}/{quote(str(key), safe='')}/delete",
                    "submit_label": f"Delete {entry.label} {key}",
                    "cancel_url": f"{root}/{quote(str(key), safe='')}",
                })

            @router.post(f"/{entry.slug}/{{pk}}/delete")
            async def delete(request: Any) -> Response:
                refusal = await _csrf_refusal(csrf, request)
                if refusal is not None:
                    return refusal
                session = open_session(request)
                try:
                    instance = await _load(session, entry, request)
                    if instance is None:
                        return _missing(entry, root, shell, self._title)
                    with actor(_actor_name(request)):
                        async with session.begin():
                            session.delete(instance)
                            await session.flush()
                    return RedirectResponse(f"{root}/", status=303)
                finally:
                    await session.close()

            _apply_access(delete_confirm, rules["delete"])
            _apply_access(delete, rules["delete"])


def _python_types(model: type, names: tuple[str, ...]) -> dict[str, Any]:
    """What each named column's submitted string has to become.

    Keyed on the declared `pg_type`, so it is the ORM's own answer rather than a
    second opinion. A type absent from `_FORM_TYPES` keeps its string, which is
    right for every text-shaped column and is what `bytea`, `uuid` and `json`
    all want -- the ORM accepts a string for those and refuses one for the rest.
    """
    columns = cast("Any", model).__wreath_column_map__
    resolved: dict[str, Any] = {}
    for name in names:
        target = _FORM_TYPES.get(getattr(columns[name].pg_type, "name", "").lower())
        if target is not None:
            resolved[name] = target
    return resolved


def _rule_for(authorize: Access | Mapping[str, Access], op: str) -> Access:
    """Resolve the rule for `op` (op > group > `"*"`), `crud`'s own precedence."""
    if isinstance(authorize, Access):
        return authorize
    if op in authorize:
        return authorize[op]
    group = "read" if op in _READ_OPERATIONS else "write"
    if group in authorize:
        return authorize[group]
    if "*" in authorize:
        return authorize["*"]
    return Access.public()


def _authorizer(request: Any) -> Any:
    return getattr(getattr(request, "app", None), "_authorizer", None)


def _actor_name(request: Any) -> str:
    """The audit actor for this request.

    Never falls back to a placeholder. `audit_log.actor` refuses an empty name,
    and an admin write with no identity is a write the route rules should
    already have refused -- so the raise here is the second line, not the first.
    """
    identity = getattr(request, "identity", None)
    subject = getattr(identity, "sub", None)
    if not subject:
        raise AdminError(
            "an admin write reached the session with no authenticated identity; "
            "every admin route carries an Access rule that refuses anonymous "
            "callers, so this is a pipeline defect rather than a policy one"
        )
    return f"user:{subject}"


def _html(template: Any, context: dict[str, Any], status: int = 200) -> Response:
    """Render one admin page and give it the headers the page owns.

    Rendering goes through `render_bytes`, which takes the context as an
    explicit mapping. `render(**context)` would have worked until a column was
    named `max_output`, at which point a row's value would have been read as the
    renderer's output limit -- the context and the parameter list share a
    namespace there, and a model's column names are not ours to constrain.

    Transport policy is not among the headers set here:
    `Strict-Transport-Security` is `SecurityHeadersPolicy`'s, because it is a
    statement about the origin rather than about this response, and an admin that
    set it would be deciding something for every other route in the deployment.
    """
    response = HTMLResponse(template.render_bytes(context).decode("utf-8"), status=status)
    response.headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))
    response.headers.append((b"referrer-policy", b"same-origin"))
    # The content type is exact and the body is generated, so a sniffed
    # alternative interpretation is never the right one.
    response.headers.append((b"x-content-type-options", b"nosniff"))
    return response


async def _csrf_refusal(csrf: Any, request: Any) -> Response | None:
    """Refuse a mutating request the verifier does not vouch for."""
    verdict = csrf(request)
    if hasattr(verdict, "__await__"):
        verdict = await verdict
    if verdict:
        return None
    return HTMLResponse(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Request refused</title></head><body><main><h1>Request refused</h1>"
        "<p>This form submission could not be verified as coming from the admin. "
        "Reload the page and try again.</p></main></body></html>",
        status=403,
    )


async def _load(session: Any, entry: ModelAdmin, request: Any) -> Any:
    return await session.get(entry.model, _coerce_pk(entry, request.path_params["pk"]))


def _coerce_pk(entry: ModelAdmin, raw: str) -> Any:
    """Path segment to primary key, driven by the declared column type."""
    column = cast("Any", entry.model).__wreath_primary_key__[0]
    name = getattr(getattr(column, "pg_type", None), "name", "").lower()
    if name not in {"int2", "int4", "int8", "smallint", "integer", "bigint"}:
        return raw
    try:
        return int(raw)
    except ValueError:
        # Not a number, so it cannot be this key: handed through so the lookup
        # misses and answers 404 rather than raising.
        return raw


def _missing(entry: ModelAdmin, root: str, shell: dict[str, Any], title: str) -> Response:
    return _html(DETAIL_TEMPLATE, {
        **shell,
        "title": f"{entry.label} not found — {title}",
        "heading": f"{entry.label} not found",
        "model_label": entry.label,
        "fields": (),
        "list_url": f"{root}/",
        "can_edit": False, "edit_url": "", "can_delete": False, "delete_url": "",
    }, status=404)


def _query_of(request: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in parse_qs(request.query_string):
        values.setdefault(key, value)
    return values


def _page_params(request: Any, entry: ModelAdmin) -> PageParams:
    """This request's page, size and sort, with the model's own default size.

    `pagination.page_params` is the general reader; the admin differs only in
    defaulting `size` per registration, so the bounds and the parse come from
    there rather than being written again.
    """
    from ..pagination import _page_params as parse_page_params

    parsed = parse_page_params(request, default_size=entry.page_size)
    sort = tuple(
        token for token in parsed.sort
        if token.lstrip("-") in entry.sortable
    )
    return PageParams(page=parsed.page, size=parsed.size, sort=sort)


def _page_url(root: str, query: dict[str, str], page: int) -> str:
    params = dict(query)
    params["page"] = str(max(1, page))
    return f"{root}/?{urlencode(params)}"


def _sort_url(root: str, query: dict[str, str], name: str, entry: ModelAdmin) -> str:
    """The link that sorts by `name`, or `""` when the column is not sortable."""
    if name not in entry.sortable:
        return ""
    params = dict(query)
    params["sort"] = f"-{name}" if query.get("sort") == name else name
    params.pop("page", None)
    return f"{root}/?{urlencode(params)}"


def _form_fields(
    entry: ModelAdmin,
    instance: Any,
    allowed: frozenset[str],
    submitted: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The controls a form draws: only columns this request may write.

    A column outside `allowed` produces no control and its value is never read
    off the instance, so an unwritable field is absent from the form rather
    than present and ignored.
    """
    columns = cast("Any", entry.model).__wreath_column_map__
    fields: list[dict[str, Any]] = []
    for name in entry.editable:
        if name not in allowed:
            continue
        column = columns[name]
        type_name = getattr(column.pg_type, "name", "text").lower()
        if submitted is not None and name in submitted:
            raw = _form_value(submitted[name])
        else:
            raw = _form_value(getattr(instance, name, None)) if instance is not None else ""
        boolean = type_name in _BOOLEAN_TYPES
        multiline = type_name in _MULTILINE_TYPES
        fields.append({
            "name": name,
            "id": f"{entry.slug}-{name}",
            "hint_id": f"{entry.slug}-{name}-hint",
            "label": _label(name),
            "type": _INPUT_TYPES.get(type_name, "text"),
            "value": raw,
            "checked": bool(raw) if boolean else False,
            "required": not column.nullable and not boolean,
            "numeric": type_name in _NUMERIC_TYPES,
            "boolean": boolean,
            "multiline": multiline,
            "plain": not boolean and not multiline,
            "hint": f"{type_name}{'' if column.nullable else ', required'}",
        })
    return fields


@dataclass(frozen=True, slots=True)
class Submission:
    """One posted form, converted -- and the text the operator actually typed.

    `raw` is kept separately because a refused submission is redrawn from it. If
    the form were repopulated from `values`, the one field that failed to convert
    would come back blank, which is the field the operator most needs to see.
    """

    values: dict[str, Any]
    raw: dict[str, str]
    problems: list[dict[str, str]]


async def _submitted(
    request: Any, entry: ModelAdmin, allowed: frozenset[str]
) -> Submission:
    """Read the posted form, keeping only columns this request may write.

    An unwritable name is dropped rather than refused, exactly as generated
    CRUD drops one: a client that sent it gets the row it would have got.

    **A form transports strings and the ORM stores typed values**, so each one
    is converted against the column's declared Python type before it reaches the
    instance. That conversion is `wreath.binding`'s, not a second one written
    here: the binding layer already turns a request string into an `int`, a
    `bool`, a `date` or an `Instant` and already says so in the words wreath uses
    for it, and a form field is the same problem as a query parameter. Without
    it every non-text column was uneditable -- the ORM refuses `'4'` for an
    `int8` -- and the admin answered 422 to a correctly filled form.
    """
    from ..binding import ValidationError

    columns = cast("Any", entry.model).__wreath_column_map__
    form = await request.form()
    values: dict[str, Any] = {}
    typed: dict[str, str] = {}
    problems: list[dict[str, str]] = []
    for name in entry.editable:
        if name not in allowed:
            continue
        column = columns[name]
        if getattr(column.pg_type, "name", "text").lower() in _BOOLEAN_TYPES:
            # A checkbox a browser omits means false. Absence is the value here,
            # so this cannot go through the string converter at all.
            values[name] = name in form
            typed[name] = "true" if name in form else ""
            continue
        if name not in form:
            continue
        raw = form[name]
        typed[name] = raw
        # An empty control on a nullable column is absence, not the empty
        # string: a browser sends "" for a field the operator cleared, and
        # storing that would turn "unknown" into "known to be blank".
        if raw == "" and column.nullable:
            values[name] = None
            continue
        try:
            values[name] = _convert(entry, name, raw)
        except ValidationError as error:
            problems.append({"message": f"{_label(name)}: {_first_detail(error)}"})
    return Submission(values=values, raw=typed, problems=problems)


def _convert(entry: ModelAdmin, name: str, raw: str) -> Any:
    """One submitted string as the column's declared Python type.

    The target comes from the **column's** `pg_type`, not from resolving the
    model's annotations: `typing.get_type_hints` needs every name in the
    annotation to be reachable from the module globals, which is not true of a
    model declared inside a function, and the failure mode there is silent --
    every column keeps its string and the form answers 422 to correct input.
    The declared column type is already the ORM's own answer and cannot fail to
    resolve.
    """
    from ..binding import _convert_scalar

    target = entry.python_types.get(name)
    if target is None:
        return raw
    if target is _Decimal:
        # `_convert_scalar` has no Decimal branch, and the ORM refuses a float
        # for `numeric` outright -- "a float cannot hold a numeric exactly" --
        # so the conversion has to land on Decimal rather than route through it.
        from ..binding import ValidationError

        try:
            return _Decimal(raw)
        except ArithmeticError:
            raise ValidationError(
                [{"loc": (name,), "msg": f"{raw!r} is not a decimal number",
                  "type": "decimal"}]
            ) from None
    return _convert_scalar(target, raw, (name,))


def _first_detail(error: Any) -> str:
    """The message out of a `ValidationError`, in the shape a form can show."""
    errors = getattr(error, "errors", None) or ()
    for item in errors:
        message = item.get("msg") if isinstance(item, dict) else None
        if message:
            return str(message)
    return "the value was not accepted"
