"""Generate REST CRUD routes from an ORM model — opt-in, and safe by default.

Auto-CRUD is convenient and dangerous: the danger is a `GET /users` that returns
everyone's `password_hash`. Wreath's version is built to make that impossible by
accident:

* **Off unless you ask** — twice. It is enabled at the app level
  (`Wreath.enable_crud()`) *and* opted into per model (you call `crud_router()` /
  `Wreath.crud()` for each one). A model is never exposed just because it exists.
* **Sensitive fields are hidden and unwritable by default.** Any column whose
  name looks like a secret — `password`, `*_hash`, `token`, `secret`,
  `salt`, `api_key`, `ssn`, … — is excluded from both responses and accepted
  input. To expose one you must name it explicitly in `expose=(...)`, an
  auditable, deliberate act. **That name check is a backstop, not a boundary**:
  it withholds the column nobody thought about, and it cannot recognise a secret
  that does not look like one. `fields=(...)` is the control.
* **Retrieval columns are withheld too, and that one is not a guess.** A
  `Vector` embedding or a `TsVector` is how rows are *found*, not what they say,
  so it is excluded from responses — a page of twenty `Vector(1536)` rows is
  thirty thousand floats — and from accepted input, because an embedding moves a
  row to the top of every semantic query while its visible content stays put.
  `expose=(...)` names one back; a generated column stays unwritable regardless.

    router = crud_router(Widget, open_session, expose=(), readonly=("owner_id",))
    app.include_router(router)          # after app.enable_crud()

Routes (any subset via `operations=`): `GET /` (paginated list),
`GET /{id}`, `POST /`, `PATCH /{id}`, `DELETE /{id}`.
"""

from __future__ import annotations

import datetime
import inspect
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from ._auth.geofence import WITHHELD as _WITHHELD
from ._auth.geofence import coarsen as _coarsen
from ._auth.geofence import resolve_precision as _resolve_precision
from .geospatial import Coordinate as _Coordinate
from .response import JSONResponse, Response

if TYPE_CHECKING:
    from .orm.model import Model


def _as_model(model: type) -> type[Model]:
    """Narrow to a wreath model so the ORM-injected class attributes resolve."""
    return cast("type[Model]", model)

__all__ = [
    "SENSITIVE_FIELD",
    "Access",
    "crud_router",
    "retrieval_fields",
    "sensitive_fields",
]


@dataclass(frozen=True, slots=True)
class Access:
    """A per-operation authorization rule for generated CRUD routes.

    Build one with a factory and hand it to `crud_router(authorize=...)`, either
    as a single rule for every operation or keyed by operation / group:

    ```python
    crud_router(Widget, open_session, authorize={
        "read":   Access.public(),            # list + retrieve
        "create": Access.roles("editor", "admin", mode="any"),
        "update": Access.roles("admin"),      # only admins
        "delete": Access.deny(),              # nobody, ever (403)
    })
    ```
    Keys may be an operation (`list`/`retrieve`/`create`/`update`/
    `delete`), a group (`read` = list+retrieve, `write` = create+update+
    delete), or `"*"` as the default. A more specific key wins.

    `Access.cedar()` attaches a policy decision that the app's configured
    `CedarAuthorizer` resolves — that authorizer (its principal/resource/entity
    mappers) is the adapter layer for richer evaluations.
    For decisions that need the *loaded row* (ownership, tenant match), pass
    `crud_router(object_authorizer=...)` instead.

    `.within(seconds)` adds step-up to any of these — `DELETE` is the operation
    that most often wants it, and it composes with the rule rather than
    replacing it.
    """

    kind: str
    values: tuple[str, ...] = ()
    mode: str = "all"
    action: str | None = None
    resource: Any = None
    #: Seconds within which the caller must have proved a second factor, or None
    #: for "no such demand". A field and a combinator rather than a seventh
    #: `kind`, because step-up is orthogonal to *who* may act: an admin-only
    #: delete that also wants a fresh factor is one rule, not a choice between
    #: two.
    second_factor: float | None = None

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
        """Callers holding these roles (`mode="all"` requires every one)."""
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
        """A Cedar policy decision (needs a configured `CedarAuthorizer`).

        `resource` is a `Type::"{id}"` template (`{id}` and other path
        params are filled in), a plain `'Type::"id"'` string, or a
        `(request) -> resource` callable.

        A string resource is parsed here, at declaration, rather than when a
        request arrives. The shape is knowable now, and a reference that cannot
        parse would otherwise surface as a 500 on a route whose declaration
        plainly intends a 403 -- the framework blaming the caller for the
        author's typo.

        Args:
            action: The Cedar action name the policy is asked about.
            resource: The entity reference, its `{param}` template, or a
                callable that builds one per request.

        Returns:
            The rule, for `crud_router(authorize=...)`.

        Raises:
            ValueError: `resource` is a string that is not a Cedar entity
                reference. Pass a callable or an `EntityUid` when a custom
                `CedarAuthorizer(resource=...)` mapper builds the reference
                from something else.
        """
        return cls("cedar", action=action, resource=_checked_resource(resource))

    def within(self, seconds: float) -> Access:
        """This rule, and a second factor proved within the last `seconds`.

        ```python
        crud_router(Account, open_session, authorize={
            "read":   Access.authenticated(),
            "delete": Access.roles("admin").within(300),   # and prove it again
        })
        ```

        A combinator rather than a factory, so it composes: the roles check
        still runs, and the window is checked on top of it by the same
        `@second_factor` machinery a hand-written route uses. Stacking two
        windows keeps the shorter one, exactly as stacking the decorators does.

        Step-up implies authentication, so this is refused on `public()` and on
        `deny()`: the first would silently stop being public, and the second
        already refuses everyone, which makes a window on it a declaration that
        can never be read.

        Args:
            seconds: The freshness window. Five minutes is the usual choice.

        Returns:
            A new rule; `Access` is frozen, so the original is unchanged.

        Raises:
            ValueError: `seconds` is not positive, or the rule is `public()` or
                `deny()`.
        """
        if seconds <= 0:
            raise ValueError(
                f"Access.within({seconds!r}) is a window no caller can satisfy. "
                "Pass a positive number of seconds, or Access.deny() if the "
                "intent is that nobody may perform this operation."
            )
        if self.kind in ("public", "deny"):
            raise ValueError(
                f"Access.{self.kind}().within(...) is a contradiction: step-up "
                "demands an identity that has recently proved a second factor, "
                f"and {self.kind}() "
                + (
                    "admits callers who have none."
                    if self.kind == "public"
                    else "admits nobody at all."
                )
                + " Build it from authenticated(), roles(), permissions() or "
                "cedar() instead."
            )
        return replace(self, second_factor=seconds)


#: A `{param}` placeholder in a resource template, filled from `path_params`.
_TEMPLATE_PARAM = re.compile(r"\{[^{}]*\}")


def _checked_resource(resource: Any) -> Any:
    """Refuse a string resource that cannot be a Cedar entity reference.

    Templates are validated by substituting each `{param}`, so `Type::"{id}"`
    is checked for shape without knowing any request's values.
    """
    if not isinstance(resource, str):
        return resource
    from ._auth.cedar_engine import CedarParseError, EntityUid

    probe = _TEMPLATE_PARAM.sub("x", resource)
    try:
        EntityUid.parse(probe)
    except CedarParseError as error:
        raise ValueError(
            f"Access.cedar(resource={resource!r}) is not a Cedar entity reference; "
            'expected Type::"id" or a Type::"{param}" template, e.g. '
            f'\'{resource}::"{{id}}"\'. Pass a callable or an EntityUid if a custom '
            "CedarAuthorizer(resource=...) mapper builds the reference instead."
        ) from error
    return resource


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
#: and rejected from input unless explicitly `expose`d.
#:
#: **This is a backstop, not a boundary.** It catches the names a secret usually
#: has, so a column nobody thought about is withheld rather than published by
#: default. It cannot catch the ones that do not look like secrets — `iban`,
#: `date_of_birth`, `tax_id`, `passport_number` — and no list of names ever will.
#: `crud_router(fields=...)` is the control: naming what may leave is the only
#: form that stays correct when somebody adds a column.
#:
#: Short tokens are anchored to a name boundary so a real column is not withheld
#: by accident: `pin` matches `pin` and `pin_code`, not `pin_board_id`. Two
#: deliberate exclusions: `public_key`, which is public by definition, and the
#: ambiguous three-letter national identifiers (`sin`, `nin`, `bic`), which occur
#: inside ordinary words too often to anchor usefully.
SENSITIVE_FIELD = re.compile(
    r"pass[_-]?(word|wd|phrase|code)|secret|token|_hash|hash_|^hash$|salt"
    r"|(private|api|access|signing|signature|encryption|master|session)[_-]?key"
    r"|credential|ssn|otp|mfa|totp|cv[cv]|security[_-]?code"
    r"|(?:^|[_-])pin([_-]?(code|number))?$|(?:^|[_-])pwd?(?:[_-]|$)|bearer"
    r"|(account|routing)[_-]?number|(recovery|backup)[_-]?(code|answer)"
    r"|security[_-]?answer",
    re.IGNORECASE,
)

_DEFAULT_OPERATIONS = ("list", "retrieve", "create", "update", "delete")
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


def sensitive_fields(model: type) -> frozenset[str]:
    """Column names of `model` that look sensitive (hidden by default)."""
    return frozenset(
        name for name in _as_model(model).__wreath_column_map__ if SENSITIVE_FIELD.search(name)
    )


def retrieval_fields(model: type) -> frozenset[str]:
    """Column names of `model` that index content rather than carry it.

    A `Vector` embedding and a `TsVector` are how rows are *found*, not what a
    row says, so generated CRUD treats them as infrastructure: withheld from
    responses and refused on input unless named in `expose=`. Unlike
    `sensitive_fields` this is not a guess from a name -- it is the declared
    column type, so it cannot miss one and cannot catch an ordinary column by
    accident.
    """
    from .orm.types import _is_retrieval_type

    return frozenset(
        name
        for name, item in _as_model(model).__wreath_column_map__.items()
        if _is_retrieval_type(item.pg_type)
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, _Coordinate):
        # Deferred to the type's own canonical REST form rather than spelled
        # again here. `Coordinate.__jsonable__` is what the JSON encoder uses,
        # so a route that serialises through CRUD and one that returns the
        # value directly cannot disagree about the shape -- which they could
        # while this branch built its own dict.
        return value.__jsonable__()
    return value


def crud_router(
    model: type,
    open_session: Callable[[Any], Any],
    *,
    prefix: str | None = None,
    operations: Iterable[str] = _DEFAULT_OPERATIONS,
    expose: Iterable[str] = (),
    fields: Iterable[str] | None = None,
    readonly: Iterable[str] = (),
    exclude: Iterable[str] = (),
    page_size: int = _DEFAULT_PAGE_SIZE,
    tags: Iterable[str] = (),
    authorize: Access | Mapping[str, Access] | None = None,
    object_authorizer: Callable[..., Any] | None = None,
    precision: Mapping[str, Any] | None = None,
) -> Any:
    """Build a `Router` of CRUD routes for `model`, mounted under `prefix`.

    `model` must have a single-column primary key; anything else raises
    `ValueError`. `prefix` defaults to the lower-cased model name, and so does
    the OpenAPI tag when `tags` is empty.

    There are two ways to control what leaves the server and they are mutually
    exclusive. `expose` is the deny-list's escape hatch: `SENSITIVE_FIELD` hides
    every column whose *name* looks like a secret, and `expose` names the ones to
    send anyway. `fields` is an allow-list, and the answer to the deny-list's
    real weakness — `iban`, `date_of_birth`, `tax_id`, and `passport_number` do
    not look like secrets, and widening the pattern until they do would start
    withholding ordinary columns instead. Naming what may leave is the only form
    that stays correct when somebody adds a column. Passing both raises
    `ValueError`, as does naming a column in `fields` that the model does not
    have.

    Sensitive columns are unwritable as well as unreadable, and `expose` does not
    change that: no CRUD route will ever set one. Change a password through a
    purpose-built endpoint, not through `PATCH`. The primary key and everything
    in `readonly` are likewise refused on input — silently dropped from the body
    rather than rejected, so a client sending them gets the row it would have got.

    **Retrieval columns are withheld both ways, and `expose` widens both.** A
    `Vector` embedding or a `TsVector` (see `retrieval_fields`) is how rows are
    *found* rather than what they say, so it is neither serialized nor accepted
    by default. On output that is bandwidth: a `tsvector` is derived from columns
    already in the same payload, and a page of twenty `Vector(1536)` rows is
    thirty thousand floats. On input it is control: ranking is the one thing a
    row's own editor can change invisibly, since an embedding puts a row at the
    top of every semantic query while the visible content stays put. An
    application whose clients compute their own embeddings names the column in
    `expose=("embedding",)`, which makes both the reading and the writing an
    explicit, auditable act. A **generated** column — every `TsVector` — has no
    such opt-in on input: PostgreSQL derives it on every write, so `expose` makes
    it readable and nothing makes it writable. It is dropped from the body like
    the primary key rather than rejected with a 422.

    `authorize` rules attach as route metadata that the app enforces in its
    single-pass pipeline, so a denied write never touches the database.
    `Access.deny()` is the exception: it is enforced inside the handler and answers
    403 whatever the identity. Rules default to `Access.public()`.

    `object_authorizer` is the seam for decisions that need the row itself —
    ownership, tenant match, a Cedar evaluation over the object's own attributes.
    It runs after the row is loaded on retrieve, update and delete, on the new
    instance before create commits, and on **every row of a list page**, which is
    why a page can come back shorter than `size`. It may be async, and it returns
    a bool or an `AuthorizationDecision` — anything falsey, or a decision whose
    `allowed` is false, answers 403.

    `page_size` sets the default page size for `GET /`. A `size` query
    parameter overrides it and is clamped to 100; `page` is clamped to
    `pagination.MAX_PAGE`, because `OFFSET` makes the database walk every
    skipped row and an unbounded page number is a table scan on request.

    Args:
        open_session: `(request) -> Session`, one per request, closed by the handler
        expose: columns withheld by default — sensitive-looking names, and
            retrieval indexes — to include anyway, an explicit opt-in
        fields: the *only* columns to serialize; mutually exclusive with `expose`
        readonly: columns excluded from create and update input, e.g. server-set ones
        exclude: columns never serialized at all
        operations: which of list/retrieve/create/update/delete to generate
        page_size: default page size for `GET /`, raisable per request up to 100
        authorize: one `Access` rule, or a mapping keyed by operation, group or `"*"`
        object_authorizer: `(request, op, instance) -> bool`, optionally async
        precision: column name to `PrecisionLadder`, making a coordinate's
            *resolution* an authorization outcome rather than a verdict — exact
            for one caller, 10 km for another, absent for a third. Applied in
            `serialize`, which is the only path a column takes out of these
            routes, so there is no select-the-column-directly way around it.

    Raises:
        ValueError: the model has a composite primary key, the field lists
            conflict, or `precision` names a column that is not serialized
    """
    from .router import Router

    spec = _as_model(model)
    columns = spec.__wreath_column_map__
    primary_key = spec.__wreath_primary_key__
    if len(primary_key) != 1:
        raise ValueError("crud_router requires a single-column primary key")
    pk_name = primary_key[0].python_name

    sensitive = sensitive_fields(model)
    # Two more deny-lists, both derived from the declared type rather than
    # guessed from a name. `retrieval` is withheld by default and `expose=` names
    # it back; `generated` can never be written by anyone, so it has no opt-in.
    retrieval = retrieval_fields(model)
    generated = frozenset(name for name, item in columns.items() if item.generated)
    exposed = frozenset(expose)
    allow_list = None if fields is None else tuple(fields)
    if allow_list is not None:
        if exposed:
            raise ValueError(
                "pass either `fields` (an allow-list of what may leave) or "
                "`expose` (exceptions to what is withheld by default -- "
                "sensitive-looking names, and retrieval columns), not both"
            )
        unknown = [name for name in allow_list if name not in columns]
        if unknown:
            raise ValueError(
                f"{model.__name__} has no column(s) {', '.join(unknown)}; "
                "`fields` names the columns to serialize"
            )
    exclude_set = frozenset(exclude)
    readonly_set = frozenset(readonly)
    ops = frozenset(operations)

    # What leaves the server: every column minus the excluded, minus sensitive
    # or retrieval ones that were not explicitly exposed. A `tsvector` in a
    # response is noise by construction -- it is derived from columns already in
    # the same payload -- and a page of twenty `Vector(1536)` rows is thirty
    # thousand floats nobody asked for.
    output_fields = (
        tuple(name for name in allow_list if name not in exclude_set)
        if allow_list is not None
        else tuple(
            name for name in columns
            if name not in exclude_set
            and (name not in sensitive or name in exposed)
            and (name not in retrieval or name in exposed)
        )
    )
    # What the client may set: never the primary key, never read-only, never a
    # sensitive column (set those through a purpose-built endpoint, not CRUD),
    # never a generated one (PostgreSQL computes it, so a write is discarded),
    # and never a retrieval index unless `expose=` named it. Ranking is the one
    # thing a row's own editor can change invisibly: an embedding puts a row at
    # the top of every semantic query while its visible content stays put.
    # `fields=` deliberately does not widen this. It names what may *leave*, and
    # a sensitive column named there is likewise readable and not writable.
    writable_fields = frozenset(
        name for name in (allow_list if allow_list is not None else columns)
        if name != pk_name
        and name not in readonly_set
        and name not in sensitive
        and name not in generated
        and (name not in retrieval or name in exposed)
    )

    resource = prefix if prefix is not None else "/" + model.__name__.lower()
    router = Router(prefix=resource.rstrip("/"), tags=tuple(tags) or (model.__name__.lower(),))

    precision_ladders = dict(precision or {})
    unknown_precision = sorted(set(precision_ladders) - set(output_fields))
    if unknown_precision:
        raise ValueError(
            "crud_router(precision=...) names columns this router does not "
            f"serialize: {', '.join(unknown_precision)}. A ladder on a column "
            "that never leaves would read as protection and protect nothing."
        )

    def serialize(instance: Any, resolutions: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The one path a column takes out of these routes.

        Every operation's response goes through here, which is what makes
        `precision` unbypassable: there is no second projection to forget, and
        a column withheld at this level cannot be reached by asking for it
        another way.
        """
        row = {name: _jsonable(getattr(instance, name)) for name in output_fields}
        if not resolutions:
            return row
        for name, metres in resolutions.items():
            value = getattr(instance, name, None)
            if metres is _WITHHELD:
                # Absent, not null: a null says "this row has no location",
                # which is a different and false statement about the world.
                row.pop(name, None)
            elif value is not None:
                row[name] = _jsonable(
                    value if metres is None else _coarsen(value, metres)
                )
        return row

    #: The Cedar resource a ladder's rungs are asked about. Resource-type level,
    #: matching the permission manifest's own split: a per-row answer would need
    #: the row before deciding, which is `object_authorizer`'s shape rather than
    #: this one. Row-level precision is a follow-up, noted in the guide.
    precision_resource = f'{model.__name__}::"*"'

    async def resolutions_for(request: Any) -> dict[str, Any] | None:
        """This request's resolution for each laddered column, or None.

        Resolved once per request rather than once per row: a list response
        would otherwise multiply `rows x rungs` authorization calls, and the
        answer cannot differ between rows of one response anyway.
        """
        if not precision_ladders:
            return None
        authorizer = getattr(getattr(request, "app", None), "_authorizer", None)
        if authorizer is None:
            # No authorizer configured: a ladder cannot be evaluated, and the
            # fail-closed answer is to withhold rather than to publish. A
            # deployment that declared a ladder and no authorizer has asked for
            # a decision nobody can make, and publishing would answer it "yes".
            return dict.fromkeys(precision_ladders, _WITHHELD)
        return {
            name: await _resolve_precision(
                request, authorizer, ladder, precision_resource
            )
            for name, ladder in precision_ladders.items()
        }

    coerce_pk = _coerce_pk_for(model)

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
                if object_authorizer is not None:
                    # The same row-level check the other operations run. Without
                    # it, a model whose rows are protected on retrieve was
                    # readable in bulk here -- the one operation that returns
                    # every row at once. A page may therefore come back shorter
                    # than `size`; that is the honest answer, and paging over a
                    # filtered set is the caller's to reconcile.
                    rows = [
                        row for row in rows
                        if not await object_denied(request, "list", row)
                    ]
                grades = await resolutions_for(request)
                return JSONResponse({
                    "items": [serialize(row, grades) for row in rows],
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
                return JSONResponse(serialize(instance, await resolutions_for(request)))
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
                    payload = serialize(instance, await resolutions_for(request))
                return JSONResponse(payload, status=201)
            except Exception as error:  # noqa: BLE001 - see `_unprocessable`
                return _unprocessable(error)
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
                    payload = serialize(instance, await resolutions_for(request))
                return JSONResponse(payload)
            except Exception as error:  # noqa: BLE001 - see `_unprocessable`
                return _unprocessable(error)
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


#: Column types whose primary key is an integer. Anything else keeps the raw
#: path segment, so a text or UUID key is not silently coerced.
_INTEGER_PG_TYPES = frozenset({"int2", "int4", "int8", "smallint", "integer", "bigint"})


def _coerce_pk_for(model: type) -> Callable[[str], Any]:
    """Build the path-segment -> primary-key conversion for `model`.

    Driven by the declared column type rather than by what the segment *looks
    like*: coercing any digit-string to `int` turned `/tokens/12` into a lookup
    for the integer 12 against a text key, which is a 500 rather than the 404
    the caller asked about.
    """
    key = _as_model(model).__wreath_primary_key__[0]
    name = getattr(getattr(key, "pg_type", None), "name", "")
    if name.lower() not in _INTEGER_PG_TYPES:
        return lambda raw: raw

    def coerce(raw: str) -> Any:
        try:
            return int(raw)
        except ValueError:
            # Not a number, so it cannot be this key -- handed through so the
            # lookup misses and answers 404.
            return raw

    return coerce


def _rule_for(authorize: Access | Mapping[str, Access] | None, op: str) -> Access:
    """Resolve the `Access` rule for `op` (op > group > `"*"` > public)."""
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
    """Attach `rule` to `handler` as auth metadata the app enforces (single-pass).

    `public` and `deny` attach nothing — `deny` is enforced inside the
    handler so it 403s regardless of identity.

    A `.within(...)` window is applied *alongside* whatever the kind produced
    rather than instead of it, which is what makes step-up compose: the roles
    check and the freshness check both end up on one `AuthRequirement`, and the
    app enforces them in one pass.
    """
    from ._auth.decorators import authenticated, authorize, permissions, roles, second_factor

    mode = cast('Literal["all", "any"]', rule.mode)
    if rule.kind == "authenticated":
        handler = authenticated()(handler)
    elif rule.kind == "roles":
        handler = roles(*rule.values, mode=mode)(handler)
    elif rule.kind == "permissions":
        handler = permissions(*rule.values, mode=mode)(handler)
    elif rule.kind == "cedar":
        handler = authorize(
            action=cast("str", rule.action), resource=_resource_fn(rule.resource)
        )(handler)
    if rule.second_factor is not None:
        handler = second_factor(max_age=rule.second_factor)(handler)
    return handler


def _resource_fn(resource: Any) -> Any:
    """Normalize a Cedar `resource` into what `@authorize` accepts."""
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

    from .pagination import MAX_PAGE

    query = parse_qs(request.query_string.decode("latin-1"))
    # Bounded above as well as below: `OFFSET (page-1)*size` makes the database
    # walk every skipped row, so an unbounded page number is a scan on request.
    page = min(MAX_PAGE, max(1, _as_int(query.get("page", ["1"])[0], 1)))
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


def _unprocessable(error: Exception) -> Response:
    """422 for a body the model would not accept.

    The exception text does not travel: only `TypeError`/`ValueError` came from
    the model's own validation and are safe to quote, and everything else here
    is a driver error whose message carries table names, SQL, and constraint
    identifiers. Catching only the first two also meant a driver error was not
    caught at all, and answered 500.
    """
    detail = str(error) if isinstance(error, (TypeError, ValueError)) else (
        "the request body was not accepted"
    )
    return JSONResponse({"error": detail}, status=422)
