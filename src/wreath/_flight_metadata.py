"""Build the deterministic NFR metadata image from a compiled application.

Runtime NFR records carry only numeric IDs; this module turns a compiled
``Wreath`` application into the canonical :class:`MetadataImage` that gives those
IDs meaning. It is pure introspection with no runtime effect: the same
application always produces a byte-identical image regardless of process,
address-space layout, or route-registration order.

IDs are assigned by *sorted content*, never registration order: every table is
collected, sorted canonically, and numbered from 1 (0 is reserved "none"). So
adding routes in a different order, or across processes, yields the same image
and therefore the same hash.

Stage 0 only. Nothing here is called on the request path.
"""

from __future__ import annotations

from typing import Any

from ._flight_schema import (
    ID_NONE,
    METADATA_VERSION,
    MetadataImage,
    NamedMeta,
    PlanMeta,
    RouteMeta,
)


def build_metadata_image(app: Any) -> MetadataImage:
    """Introspect a compiled ``Wreath`` app into a canonical metadata image."""
    from ._auth.requirements import merge_requirements, requirement_for
    from .binding import inspect_handler

    routes = getattr(app, "_routes", ())
    databases = getattr(app, "_databases", {}) or {}
    orm_registries = getattr(app, "_orm_registries", {}) or {}

    # Interners collect names first; IDs are assigned after sorting so order of
    # discovery cannot affect the result.
    dependencies = _Interner()
    middleware = _Interner()
    auth_policies = _Interner()
    serializers = _Interner()
    validators = _Interner()
    limits = _Interner()
    clients = _Interner()
    components = _Interner()

    databases_table = _Interner()
    for name in databases:
        databases_table.intern(str(name))
    for name in getattr(app, "_http_clients", {}) or {}:
        clients.intern(str(name))
    models_table = _Interner()
    for registry in orm_registries.values():
        for model_name in _model_names(registry):
            models_table.intern(model_name)

    # App-level middleware are recorded as components (they wrap every route).
    for item in getattr(app, "_middleware", ()):  # (order, seq, middleware)
        components.intern(_callable_name(item[2] if isinstance(item, tuple) else item))

    # First pass: intern every referenced name and build per-route descriptors
    # keyed by content, so IDs can be assigned deterministically afterwards.
    raw_routes: list[dict[str, Any]] = []
    plan_keys: dict[tuple, dict[str, Any]] = {}

    for definition in routes:
        spec = None
        try:
            spec = inspect_handler(definition.endpoint, definition.path)
        except TypeError:
            # `inspect_handler` already returns None for anything it cannot
            # inspect; the only thing it *raises* is TypeError, for a handler
            # it judges invalid (`*args`/`**kwargs`, a bare `Session`). Such a
            # route cannot serve either, so the app is already broken -- the
            # image just declines to describe it rather than failing the build
            # a second time. Anything else escaping is a bug in the inspector
            # and is no longer swallowed here.
            spec = None

        dep_names = sorted(
            {_callable_name(dep.fn) for dep in getattr(definition, "dependencies", ())}
            | ({_callable_name(d[1].fn) for d in getattr(spec, "depends", ())} if spec else set())
        )
        for name in dep_names:
            dependencies.intern(name)

        mw_names = sorted({_callable_name(mw) for mw in getattr(definition, "middleware", ())})
        for name in mw_names:
            middleware.intern(name)

        requirement = merge_requirements(
            definition.requirement, requirement_for(definition.endpoint)
        )
        auth_name = _auth_policy_name(requirement)
        if auth_name is not None:
            auth_policies.intern(auth_name)

        plan_key, plan_desc = _plan_descriptor(spec, serializers, validators, limits)
        plan_keys.setdefault(plan_key, plan_desc)

        for method in definition.methods:
            raw_routes.append(
                {
                    "method": method.upper(),
                    "path": definition.path,
                    "operation_id": _operation_id(definition, method),
                    "tags": tuple(getattr(definition, "tags", ())),
                    "dep_names": dep_names,
                    "mw_names": mw_names,
                    "auth_name": auth_name,
                    "plan_key": plan_key,
                    "coverage": _coverage(spec),
                }
            )

    # WebSocket routes are inspectable routes too (method WEBSOCKET). They are not
    # HTTP-request-shaped, so they carry no endpoint plan (plan_id stays ID_NONE);
    # only their auth policy is interned. Enumerated from the app's WS route list,
    # not the router, so the image stays deterministic and address-free.
    for ws_path, ws_handler in getattr(app, "_ws_routes", ()) or ():
        ws_auth_name = None
        try:
            ws_auth_name = _auth_policy_name(requirement_for(ws_handler))
        except AttributeError:
            # `requirement_for` is a `getattr` with a default and cannot fail on
            # an ordinary object; only a handler with a hostile `__getattr__`
            # reaches here. Narrowed from a blanket catch so a genuine bug in
            # `_auth_policy_name` surfaces instead of quietly unnaming a policy.
            ws_auth_name = None
        if ws_auth_name is not None:
            auth_policies.intern(ws_auth_name)
        raw_routes.append(
            {
                "method": "WEBSOCKET",
                "path": ws_path,
                "operation_id": _ws_operation_id(ws_path),
                "tags": (),
                "dep_names": [],
                "mw_names": [],
                "auth_name": ws_auth_name,
                "plan_key": None,  # WS handlers carry no HTTP endpoint plan
                "coverage": "python",
            }
        )

    # Assign IDs (sorted) to each named table.
    dep_ids = dependencies.assign()
    mw_ids = middleware.assign()
    auth_ids = auth_policies.assign()
    ser_ids = serializers.assign()
    val_ids = validators.assign()
    lim_ids = limits.assign()
    client_ids = clients.assign()
    db_ids = databases_table.assign()
    model_ids = models_table.assign()
    comp_ids = components.assign()

    # Assign plan IDs by canonical plan key order.
    plan_id_by_key: dict[tuple, int] = {
        key: index + 1 for index, key in enumerate(sorted(plan_keys))
    }
    plans = tuple(
        PlanMeta(
            plan_id=plan_id_by_key[key],
            params=desc["params"],
            body_type=desc["body_type"],
            returns_type=desc["returns_type"],
            serializer_id=ser_ids.get(desc["serializer_name"], ID_NONE),
            validator_id=val_ids.get(desc["validator_name"], ID_NONE),
            limit_ids=tuple(sorted(lim_ids[name] for name in desc["limit_names"])),
        )
        for key, desc in sorted(plan_keys.items())
    )

    # Assign route IDs by canonical (method, path) order.
    ordered = sorted(raw_routes, key=lambda r: (r["path"], r["method"]))
    route_metas = tuple(
        RouteMeta(
            route_id=index + 1,
            method=r["method"],
            path=r["path"],
            operation_id=r["operation_id"],
            plan_id=plan_id_by_key[r["plan_key"]] if r["plan_key"] is not None else ID_NONE,
            tags=r["tags"],
            dependency_ids=tuple(sorted(dep_ids[name] for name in r["dep_names"])),
            middleware_ids=tuple(sorted(mw_ids[name] for name in r["mw_names"])),
            auth_policy_id=auth_ids.get(r["auth_name"], ID_NONE) if r["auth_name"] else ID_NONE,
            coverage=r["coverage"],
        )
        for index, r in enumerate(ordered)
    )

    return MetadataImage(
        version=METADATA_VERSION,
        routes=route_metas,
        plans=plans,
        dependencies=_named(dep_ids),
        middleware=_named(mw_ids),
        auth_policies=_named(auth_ids),
        serializers=_named(ser_ids),
        validators=_named(val_ids),
        limits=_named(lim_ids),
        clients=_named(client_ids),
        databases=_named(db_ids),
        models=_named(model_ids),
        components=_named(comp_ids),
    )


class _Interner:
    """Collects names, then assigns deterministic IDs in sorted order."""

    __slots__ = ("_names", "_ids")

    def __init__(self) -> None:
        self._names: set[str] = set()
        self._ids: dict[str, int] = {}

    def intern(self, name: str) -> None:
        self._names.add(name)

    def assign(self) -> dict[str, int]:
        self._ids = {name: index + 1 for index, name in enumerate(sorted(self._names))}
        return self._ids


def _named(ids: dict[str, int]) -> tuple[NamedMeta, ...]:
    return tuple(
        NamedMeta(entry_id=entry_id, name=name)
        for name, entry_id in sorted(ids.items(), key=lambda kv: kv[1])
    )


def _plan_descriptor(
    spec: Any, serializers: _Interner, validators: _Interner, limits: _Interner
) -> tuple[tuple, dict[str, Any]]:
    """Build an immutable endpoint-plan descriptor beside the handler closures."""
    params: list[tuple[str, str, str]] = []
    limit_names: list[str] = []
    body_type = ""
    returns_type = ""
    serializer_name = ""
    validator_name = ""

    if spec is not None:
        for kind, rows in (
            ("path", getattr(spec, "path_params", ())),
            ("query", getattr(spec, "query_params", ())),
            ("header", getattr(spec, "header_params", ())),
            ("cookie", getattr(spec, "cookie_params", ())),
            ("form", getattr(spec, "form_params", ())),
            ("file", getattr(spec, "file_params", ())),
        ):
            for row in rows:
                name = row[0]
                type_obj = row[2] if len(row) > 2 else None
                params.append((name, kind, _type_name(type_obj)))
        body = getattr(spec, "body", None)
        if body is not None:
            body_type = _type_name(body[1])
            validator_name = f"validate:{body_type}"
            validators.intern(validator_name)
        returns = getattr(spec, "returns", None)
        returns_type = _type_name(returns)
        if returns_type not in ("", "None", "NoneType"):
            serializer_name = f"serialize:{returns_type}"
            serializers.intern(serializer_name)
        for name, _constraint in getattr(spec, "query_constraints", ()):
            limit_name = f"limit:query:{name}"
            limit_names.append(limit_name)
            limits.intern(limit_name)

    params.sort()
    limit_names.sort()
    key = (
        tuple(params),
        body_type,
        returns_type,
        serializer_name,
        validator_name,
        tuple(limit_names),
    )
    desc = {
        "params": tuple(params),
        "body_type": body_type,
        "returns_type": returns_type,
        "serializer_name": serializer_name,
        "validator_name": validator_name,
        "limit_names": limit_names,
    }
    return key, desc


def _auth_policy_name(requirement: Any) -> str | None:
    authenticated = bool(getattr(requirement, "authenticated", False))
    role_checks = getattr(requirement, "role_checks", ())
    permission_checks = getattr(requirement, "permission_checks", ())
    policies = getattr(requirement, "policies", ())
    if not (authenticated or role_checks or permission_checks or policies):
        return None
    parts: list[str] = []
    if authenticated:
        parts.append("auth")
    for check in role_checks:
        values = ",".join(sorted(check.values))
        parts.append(f"role:{check.mode}:{values}")
    for check in permission_checks:
        values = ",".join(sorted(check.values))
        parts.append(f"perm:{check.mode}:{values}")
    for policy in policies:
        parts.append(f"policy:{_callable_name(policy)}")
    parts.sort()
    return "|".join(parts)


def _coverage(spec: Any) -> str:
    # Wreath enters Python for application/handler execution today; a typed
    # handler is "mixed" (native transport + Python binding/handler), an
    # untyped request-only handler is "python".
    return "mixed" if spec is not None else "python"


def _operation_id(definition: Any, method: str) -> str:
    explicit = getattr(definition, "operation_id", None)
    if explicit:
        return str(explicit)
    # Deterministic fallback mirroring the typegen/OpenAPI derivation shape.
    path = definition.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method.lower()}_{path}" if path else method.lower()


def _ws_operation_id(path: str) -> str:
    """Deterministic operation ID for a WebSocket route (no HTTP method)."""
    slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"websocket_{slug}" if slug else "websocket"


def _model_names(registry: Any) -> list[str]:
    """The model class names a registry holds, for the image's models table.

    A registry exposes ``specs`` (a tuple of ``ModelSpec``) whose ``model_type``
    is the class; the other attribute names are tolerated so a registry-shaped
    test double or a future container still resolves. Anything that yields
    neither a class nor a spec is skipped rather than guessed at.
    """
    names: set[str] = set()
    for attr in ("specs", "_specs", "models", "_models"):
        container = getattr(registry, attr, None)
        if container is None:
            continue
        try:
            items = container.values() if hasattr(container, "values") else container
        except (AttributeError, TypeError):
            # `hasattr` already guards absence, so this only covers a `values`
            # that exists and misbehaves -- a test double, or a container whose
            # attribute is not callable. Narrowed from a blanket catch: a
            # registry raising anything else is a real fault, not a shape to
            # tolerate.
            continue
        for item in items:
            model = getattr(item, "model_type", None) or getattr(item, "model", None)
            source = model if model is not None else item
            name = getattr(source, "__qualname__", None) or getattr(
                source, "__name__", None
            )
            if name:
                names.add(str(name))
    return sorted(names)


def _callable_name(obj: Any) -> str:
    for attr in ("__qualname__", "__name__"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    cls = type(obj)
    return getattr(cls, "__qualname__", None) or cls.__name__


def _type_name(obj: Any) -> str:
    if obj is None:
        return "None"
    for attr in ("__qualname__", "__name__"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    # typing constructs (list[int], X | None): str() is stable and address-free.
    return str(obj)
