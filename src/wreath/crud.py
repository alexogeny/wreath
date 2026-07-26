"""Generate REST CRUD routes from an ORM model — opt-in, and safe by default.

Auto-CRUD is convenient and dangerous: the danger is a `GET /users` that returns
everyone's `password_hash`. Wreath's version is built to make that impossible by
accident:

* **Off unless you ask** — twice. It is enabled at the app level
  (:meth:`Wreath.enable_crud`) *and* opted into per model (you call
  :func:`crud_router` / :meth:`Wreath.crud` for each one). A model is never
  exposed just because it exists.
* **Sensitive fields are hidden and unwritable by default.** Any column whose
  name looks like a secret — ``password``, ``*_hash``, ``token``, ``secret``,
  ``salt``, ``api_key``, ``ssn``, … — is excluded from both responses and accepted
  input. To expose one you must name it explicitly in ``expose=(...)``, an
  auditable, deliberate act.

    router = crud_router(Widget, open_session, expose=(), readonly=("owner_id",))
    app.include_router(router)          # after app.enable_crud()

Routes (any subset via ``operations=``): ``GET /`` (paginated list),
``GET /{id}``, ``POST /``, ``PATCH /{id}``, ``DELETE /{id}``.
"""

from __future__ import annotations

import datetime
import inspect
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from .response import JSONResponse, Response

if TYPE_CHECKING:
    from .orm.model import Model


def _as_model(model: type) -> type[Model]:
    """Narrow to a wreath model so the ORM-injected class attributes resolve."""
    return cast("type[Model]", model)

__all__ = ["SENSITIVE_FIELD", "Access", "crud_router", "sensitive_fields"]


@dataclass(frozen=True, slots=True)
class Access:
    """A per-operation authorization rule for generated CRUD routes.

    Build one with a factory and hand it to ``crud_router(authorize=...)``, either
    as a single rule for every operation or keyed by operation / group::

        crud_router(Widget, open_session, authorize={
            "read":   Access.public(),            # list + retrieve
            "create": Access.roles("editor", "admin", mode="any"),
            "update": Access.roles("admin"),      # only admins
            "delete": Access.deny(),              # nobody, ever (403)
        })

    Keys may be an operation (``list``/``retrieve``/``create``/``update``/
    ``delete``), a group (``read`` = list+retrieve, ``write`` = create+update+
    delete), or ``"*"`` as the default. A more specific key wins.

    :meth:`cedar` attaches a policy decision that the app's configured
    :class:`~wreath.authorization.CedarAuthorizer` resolves — that authorizer (its
    principal/resource/entity mappers) is the adapter layer for richer evaluations.
    For decisions that need the *loaded row* (ownership, tenant match), pass
    ``crud_router(object_authorizer=...)`` instead.
    """

    kind: str
    values: tuple[str, ...] = ()
    mode: str = "all"
    action: str | None = None
    resource: Any = None

    @classmethod
    def public(cls) -> Access:
        """Anyone, authenticated or not."""
        return cls("public")

    @classmethod
    def authenticated(cls) -> Access:
        """Any authenticated identity."""
        return cls("authenticated")

    @classmethod
    def roles(cls, *names: str, mode: str = "all") -> Access:
        """Callers holding these roles (``mode="all"`` requires every one)."""
        return cls("roles", tuple(names), _mode(mode))

    @classmethod
    def permissions(cls, *names: str, mode: str = "all") -> Access:
        """Callers holding these permissions."""
        return cls("permissions", tuple(names), _mode(mode))

    @classmethod
    def deny(cls) -> Access:
        """Nobody — the route exists but always answers 403."""
        return cls("deny")

    @classmethod
    def cedar(cls, *, action: str, resource: Any) -> Access:
        """A Cedar policy decision (needs a configured ``CedarAuthorizer``).

        ``resource`` is a ``Type::"{id}"`` template (``{id}`` and other path
        params are filled in), a plain ``'Type::"id"'`` string, or a
        ``(request) -> resource`` callable.
        """
        return cls("cedar", action=action, resource=resource)


def _mode(mode: str) -> str:
    if mode not in ("all", "any"):
        raise ValueError(f"mode must be 'all' or 'any', not {mode!r}")
    return mode


#: Which group each operation belongs to, for `authorize={"read": ...}` keys.
_OP_GROUP = {
    "list": "read", "retrieve": "read",
    "create": "write", "update": "write", "delete": "write",
}

#: A column whose name matches this is treated as a secret: hidden from output
#: and rejected from input unless explicitly ``expose``d.
SENSITIVE_FIELD = re.compile(
    r"pass(word|wd|phrase)|secret|token|_hash|hash_|^hash$|salt|private[_-]?key"
    r"|api[_-]?key|credential|ssn|otp|mfa|totp|cvv|security[_-]?code",
    re.IGNORECASE,
)

_DEFAULT_OPERATIONS = ("list", "retrieve", "create", "update", "delete")
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


def sensitive_fields(model: type) -> frozenset[str]:
    """Column names of ``model`` that look sensitive (hidden by default)."""
    return frozenset(
        name for name in _as_model(model).__wreath_column_map__ if SENSITIVE_FIELD.search(name)
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def crud_router(
    model: type,
    open_session: Callable[[Any], Any],
    *,
    prefix: str | None = None,
    operations: Iterable[str] = _DEFAULT_OPERATIONS,
    expose: Iterable[str] = (),
    readonly: Iterable[str] = (),
    exclude: Iterable[str] = (),
    page_size: int = _DEFAULT_PAGE_SIZE,
    tags: Iterable[str] = (),
    authorize: Access | Mapping[str, Access] | None = None,
    object_authorizer: Callable[..., Any] | None = None,
) -> Any:
    """Build a :class:`~wreath.router.Router` of CRUD routes for ``model``.

    Args:
        open_session: ``(request) -> Session`` — a fresh ORM session per request;
            the CRUD handlers close it when done.
        expose: sensitive columns to include in responses anyway (explicit opt-in).
        readonly: columns excluded from create/update input (e.g. server-set).
        exclude: columns never serialized at all.
        operations: which of list/retrieve/create/update/delete to generate.
        authorize: an :class:`Access` rule for every operation, or a mapping keyed
            by operation / group / ``"*"``. Roles, permissions, and Cedar policies
            attach as route metadata the app enforces in its single-pass pipeline
            (a denied write never touches the database); ``Access.deny()`` answers
            403 unconditionally. Rules default to :meth:`Access.public`.
        object_authorizer: ``(request, op, instance) -> bool | AuthorizationDecision``
            (optionally async) run *after* the row is loaded on retrieve / update /
            delete and on the new instance for create — the seam for row-level
            checks (ownership, tenant) and richer Cedar evaluations that need the
            object's own attributes. Returning falsey answers 403.
    """
    from .router import Router

    spec = _as_model(model)
    columns = spec.__wreath_column_map__
    primary_key = spec.__wreath_primary_key__
    if len(primary_key) != 1:
        raise ValueError("crud_router requires a single-column primary key")
    pk_name = primary_key[0].python_name

    sensitive = sensitive_fields(model)
    exposed_sensitive = frozenset(expose)
    exclude_set = frozenset(exclude)
    readonly_set = frozenset(readonly)
    ops = frozenset(operations)

    # What leaves the server: every column minus the excluded, minus sensitive
    # ones that were not explicitly exposed.
    output_fields = tuple(
        name for name in columns
        if name not in exclude_set and (name not in sensitive or name in exposed_sensitive)
    )
    # What the client may set: never the primary key, never read-only, never a
    # sensitive column (set those through a purpose-built endpoint, not CRUD).
    writable_fields = frozenset(
        name for name in columns
        if name != pk_name and name not in readonly_set and name not in sensitive
    )

    resource = prefix if prefix is not None else "/" + model.__name__.lower()
    router = Router(prefix=resource.rstrip("/"), tags=tuple(tags) or (model.__name__.lower(),))

    def serialize(instance: Any) -> dict[str, Any]:
        return {name: _jsonable(getattr(instance, name)) for name in output_fields}

    def coerce_pk(raw: str) -> Any:
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def clean_input(body: Any) -> dict[str, Any] | None:
        if not isinstance(body, dict):
            return None
        return {k: v for k, v in body.items() if k in writable_fields}

    rules = {op: _rule_for(authorize, op) for op in _DEFAULT_OPERATIONS}

    async def object_denied(request: Any, op: str, instance: Any) -> bool:
        return object_authorizer is not None and not await _object_ok(
            object_authorizer, request, op, instance)

    if "list" in ops:
        list_rule = rules["list"]

        @router.get("/")
        async def list_(request: Any) -> Response:
            if list_rule.kind == "deny":
                return _forbidden()
            session = open_session(request)
            try:
                page, size = _page_params(request, page_size)
                query = spec.select().limit(size).offset((page - 1) * size)
                rows = await session.fetch(query)
                return JSONResponse({
                    "items": [serialize(row) for row in rows],
                    "page": page, "size": size,
                })
            finally:
                await session.close()
        _apply_requirement(list_, list_rule)

    if "retrieve" in ops:
        retrieve_rule = rules["retrieve"]

        @router.get("/{id}")
        async def retrieve(request: Any) -> Response:
            if retrieve_rule.kind == "deny":
                return _forbidden()
            session = open_session(request)
            try:
                instance = await session.get(model, coerce_pk(request.path_params["id"]))
                if instance is None:
                    return _not_found()
                if await object_denied(request, "retrieve", instance):
                    return _forbidden()
                return JSONResponse(serialize(instance))
            finally:
                await session.close()
        _apply_requirement(retrieve, retrieve_rule)

    if "create" in ops:
        create_rule = rules["create"]

        @router.post("/")
        async def create(request: Any) -> Response:
            if create_rule.kind == "deny":
                return _forbidden()
            values = clean_input(await request.json())
            if values is None:
                return _bad_request("request body must be a JSON object")
            session = open_session(request)
            try:
                instance = model(**values)
                if await object_denied(request, "create", instance):
                    return _forbidden()
                async with session.begin():
                    session.add(instance)
                    await session.flush()
                    payload = serialize(instance)
                return JSONResponse(payload, status=201)
            except (TypeError, ValueError) as error:
                return _unprocessable(str(error))
            finally:
                await session.close()
        _apply_requirement(create, create_rule)

    if "update" in ops:
        update_rule = rules["update"]

        @router.patch("/{id}")
        async def update(request: Any) -> Response:
            if update_rule.kind == "deny":
                return _forbidden()
            values = clean_input(await request.json())
            if values is None:
                return _bad_request("request body must be a JSON object")
            session = open_session(request)
            try:
                instance = await session.get(model, coerce_pk(request.path_params["id"]))
                if instance is None:
                    return _not_found()
                if await object_denied(request, "update", instance):
                    return _forbidden()
                async with session.begin():
                    for name, value in values.items():
                        setattr(instance, name, value)
                    await session.flush()
                    payload = serialize(instance)
                return JSONResponse(payload)
            except (TypeError, ValueError) as error:
                return _unprocessable(str(error))
            finally:
                await session.close()
        _apply_requirement(update, update_rule)

    if "delete" in ops:
        delete_rule = rules["delete"]

        @router.delete("/{id}")
        async def delete(request: Any) -> Response:
            if delete_rule.kind == "deny":
                return _forbidden()
            session = open_session(request)
            try:
                instance = await session.get(model, coerce_pk(request.path_params["id"]))
                if instance is None:
                    return _not_found()
                if await object_denied(request, "delete", instance):
                    return _forbidden()
                async with session.begin():
                    session.delete(instance)
                    await session.flush()
                return Response(b"", status=204)
            finally:
                await session.close()
        _apply_requirement(delete, delete_rule)

    return router


def _rule_for(authorize: Access | Mapping[str, Access] | None, op: str) -> Access:
    """Resolve the :class:`Access` rule for ``op`` (op > group > ``"*"`` > public)."""
    if authorize is None:
        return Access.public()
    if isinstance(authorize, Access):
        return authorize
    if op in authorize:
        return authorize[op]
    group = _OP_GROUP.get(op)
    if group is not None and group in authorize:
        return authorize[group]
    if "*" in authorize:
        return authorize["*"]
    return Access.public()


def _apply_requirement(handler: Any, rule: Access) -> Any:
    """Attach ``rule`` to ``handler`` as auth metadata the app enforces (single-pass).

    ``public`` and ``deny`` attach nothing — ``deny`` is enforced inside the
    handler so it 403s regardless of identity.
    """
    from ._auth.decorators import authenticated, authorize, permissions, roles

    if rule.kind == "authenticated":
        return authenticated()(handler)
    mode = cast('Literal["all", "any"]', rule.mode)
    if rule.kind == "roles":
        return roles(*rule.values, mode=mode)(handler)
    if rule.kind == "permissions":
        return permissions(*rule.values, mode=mode)(handler)
    if rule.kind == "cedar":
        return authorize(action=cast("str", rule.action), resource=_resource_fn(rule.resource))(
            handler)
    return handler


def _resource_fn(resource: Any) -> Any:
    """Normalize a Cedar ``resource`` into what ``@authorize`` accepts."""
    if callable(resource):
        return resource
    if isinstance(resource, str) and "{" in resource:
        return lambda request: resource.format(**request.path_params)
    return resource


async def _object_ok(
    authorizer: Callable[..., Any], request: Any, op: str, instance: Any,
) -> bool:
    """Run a row-level authorizer; accept a bool or an AuthorizationDecision."""
    result = authorizer(request, op, instance)
    if inspect.isawaitable(result):
        result = await result
    allowed = getattr(result, "allowed", None)
    return bool(result) if allowed is None else bool(allowed)


def _page_params(request: Any, default_size: int) -> tuple[int, int]:
    from urllib.parse import parse_qs

    query = parse_qs(request.query_string.decode("latin-1"))
    page = max(1, _as_int(query.get("page", ["1"])[0], 1))
    raw_size = _as_int(query.get("size", [str(default_size)])[0], default_size)
    return page, min(_MAX_PAGE_SIZE, max(1, raw_size))


def _as_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _not_found() -> Response:
    return JSONResponse({"error": "not found"}, status=404)


def _forbidden() -> Response:
    return JSONResponse({"error": "forbidden"}, status=403)


def _bad_request(detail: str) -> Response:
    return JSONResponse({"error": detail}, status=400)


def _unprocessable(detail: str) -> Response:
    return JSONResponse({"error": detail}, status=422)
