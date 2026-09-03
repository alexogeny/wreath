"""GraphQL over the Wreath ORM, sharing everything the rest of the app uses.

Point it at a registry and it derives the schema from your models -- the same
`ModelSpec` the SQL compiler, OpenAPI, and typegen read, so the GraphQL
surface cannot drift from the REST one:

```python
from wreath.graphql import GraphQL

api = GraphQL(app.orm("main"), models=[User, Post])
app.include_router(api.router())
```
What owning this in-tree buys, none of which a bolt-on library can:

* **No N+1 and no DataLoader.** A relationship selection resolves through the
  session's batched select-in loader -- one statement per relationship per
  level, deduplicated by the identity map.
* **One authorization language.** Field access is checked against the
  authorizer you pass, with `Type.field` as the resource, so one Cedar policy
  set covers REST and GraphQL. The authorizer is asked the way a route asks it
  -- a `PolicyRequirement` whose resource is the field as a Cedar entity
  reference, `User::"email"` -- so the shipped `CedarAuthorizer` needs no
  adapter. Hand it the same object you handed `configure_auth`; there is
  deliberately no pickup from the application.
* **Per-field latency in the Flight Recorder**, as `RESOLVER` phases, without
  wiring an exporter.
* **Typegen.** `wreath typegen` emits GraphQL operations alongside the REST
  ones, sharing one TypeScript model set, so a client gets `useGetUser()` and
  `useUserQuery()` returning the same `User`.

Safety is not optional here. A public GraphQL endpoint is a denial-of-service
surface, so depth, complexity, alias, and parse-step limits are enforced *while
parsing* and cannot be turned off -- only widened. Introspection is off by
default, because a schema dump is reconnaissance.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ._auth.permissions import SURFACE_ACTIONS
from ._graphql.cost import weigh
from ._graphql.execute import ExecutionError, execute, execute_json
from ._graphql.parser import GraphQLSyntaxError, Limits, parse
from ._graphql.resolvers import (
    ResolverError,
    ResolverInfo,
    ResolverRegistry,
    ResolverSpec,
    validate_dependencies,
)
from ._graphql.schema import RootField, Schema, SchemaField, build_schema, policy_resource
from ._native import _core
from .cache import BoundedCache
from .request import Request
from .response import JSONResponse, Response
from .router import Router

__all__ = [
    "MAX_CACHED_QUERY_CHARS",
    "ExecutionError",
    "GraphQL",
    "GraphQLSyntaxError",
    "Limits",
    "ResolverError",
    "ResolverInfo",
    "Schema",
    "build_schema",
]


#: Longest document the parse cache will hold. Past this the query is still
#: served -- it is parsed every time instead of being remembered.
MAX_CACHED_QUERY_CHARS = 16 * 1024

#: Content types the POST endpoint accepts. Deliberately excludes the three a
#: cross-origin <form> can send (`text/plain`, `application/x-www-form-urlencoded`,
#: `multipart/form-data`), which is what keeps a simple request from reaching a
#: mutation with the caller's cookies attached.
_ACCEPTED_CONTENT_TYPES = frozenset({"application/json", "application/graphql+json"})


@dataclass(frozen=True, slots=True)
class _CachedDocument:
    """One parsed document and the operations already charged against its schema.

    The entry, rather than a second cache, keeps the cost fact under the parsed
    document's existing client-text bound.  Replacing the immutable entry is
    also safe when free-threaded requests race: at worst both perform the same
    bounded walk once, and neither can observe a half-written set.
    """

    document: Any
    weighed: frozenset[str | None] = frozenset()


class GraphQL:
    """A GraphQL endpoint over one ORM registry."""

    __slots__ = (
        "_action",
        "_authorizer",
        "_cache",
        "_frozen",
        "_introspection",
        "_limits",
        "_max_page_size",
        "_on_denied",
        "_registry",
        "_resolvers",
        "_schema",
        "_policy_schema",
        "_session_factory",
        "resolver_errors",
    )

    def __init__(
        self,
        registry: Any,
        *,
        models: list[Any] | None = None,
        dataclasses: Iterable[type] = (),
        expose: Iterable[str] = (),
        limits: Limits | None = None,
        authorizer: Any = None,
        action: str = "read",
        introspection: bool = False,
        max_page_size: int = 100,
        cache_size: int = 512,
        on_denied: str = "error",
    ) -> None:
        """Serve `registry` as a GraphQL endpoint.

        `authorizer` is an `AuthorizationProvider` -- the same object the
        application was configured with, passed rather than discovered. Every
        field, relationship and root is asked about with one
        `PolicyRequirement(action, resource)`, where `action` is this argument
        and the resource is the field's policy as a Cedar entity reference.
        `"read"` is the action because a selection is a read; a mutation is
        distinguished by its resource (`Mutation.createUser`), not by a second
        action, which is what lets `resource is Mutation` deny every write in
        one clause.

        Raises:
            ValueError: `on_denied` is not `"error"` or `"null"`, or `action`
                is empty -- an unnamed action would reach Cedar as
                `Action::""`, which no policy can match, so every field would
                deny with nothing to point at.
        """
        if on_denied not in ("error", "null"):
            raise ValueError("on_denied must be 'error' or 'null'")
        if not action:
            raise ValueError("authorization action is required")
        self._action = action
        self._registry = registry
        self._schema = build_schema(registry, models, expose=expose, dataclasses=dataclasses)
        self._limits = limits or Limits()
        self._authorizer = authorizer
        self._introspection = introspection
        self._max_page_size = max_page_size
        self._on_denied = on_denied
        self._resolvers = ResolverRegistry()
        self._frozen = False
        self._policy_schema: Any = None
        # Parsed documents are immutable, so a repeated query skips the parse
        # (and its limit checks) entirely. Bounded, because the key is
        # client-supplied text -- an unbounded cache here is a memory DoS.
        self._cache: BoundedCache = BoundedCache(max_entries=cache_size)
        self._session_factory: Any = None
        #: Resolver failures that were not `ExecutionError`. The message a
        #: client gets is generic, so this is where the count lives.
        self.resolver_errors = 0

    @property
    def schema(self) -> Schema:
        return self._schema

    def field(
        self,
        type_name: str,
        field_name: str,
        *,
        returns: str = "String",
        is_list: bool = False,
        non_null: bool = False,
        requires: tuple[str, ...] | list[str] = (),
        batch: bool = True,
        policy: str | None = None,
        cost: int = 1,
    ):
        """Register a computed field on `type_name`.

        The resolver is **batched by default**: it receives the whole level as
        a list and returns one value per object. Per-parent resolvers are how
        application code reintroduces the N+1 the data layer just solved, so
        the batched form is the easy one to write:

        ```python
        @api.field("User", "postCount", returns="Int", requires=["posts"])
        async def post_count(users, info):
            return [len(user.posts) for user in users]
        ```
        `requires` names sibling fields that must be resolved first. They are
        resolved in batch, and if the client did not select them they stay
        hidden -- asking for a computed field never widens the response.

        Pass `batch=False` for the one-object-at-a-time form when the work is
        genuinely per-object and cannot be batched.
        """

        def register(fn):
            self._add_resolver(
                ResolverSpec(
                    type_name=type_name,
                    field_name=field_name,
                    fn=fn,
                    requires=tuple(requires),
                    batch=batch,
                    type_name_out=returns,
                    is_list=is_list,
                    non_null=non_null,
                    policy=policy,
                    cost=cost,
                )
            )
            return fn

        return register

    def query(
        self,
        name: str,
        *,
        returns: str,
        is_list: bool = False,
        policy: str | None = None,
        cost: int = 10,
    ):
        """Register a custom root query field.

        Needs no backing table: use it for a search, an aggregate, or anything
        assembled from more than one model:

        ```python
        @api.query("search", returns="User", is_list=True)
        async def search(info):
            return await info.session.fetch(
                User.select().where(User.email.like(info.arguments["term"]))
            )
        ```
        """

        def register(fn):
            self._add_root(name, fn, returns, is_list, policy, cost, mutation=False)
            return fn

        return register

    def mutation(
        self,
        name: str,
        *,
        returns: str,
        is_list: bool = False,
        policy: str | None = None,
        cost: int = 10,
    ):
        """Register a mutation.

        Mutations are separate from queries on purpose: they are the write
        surface, and giving them their own namespace means an authorization
        policy can cover *all* writes (`Mutation.*`) without enumerating them.
        """

        def register(fn):
            self._add_root(name, fn, returns, is_list, policy, cost, mutation=True)
            return fn

        return register

    @staticmethod
    def _checked_policy(policy: str | None) -> str | None:
        """Refuse a policy resource no authorizer could evaluate, at declaration.

        The derived `Type.field` resources are always readable; a hand-written
        one need not be. Parsing it here means a typo is a startup error rather
        than a generic resolver failure on the first request that selects the
        field -- the endpoint blaming the caller for the author's mistake.
        """
        if policy is not None:
            policy_resource(policy)
        return policy

    @staticmethod
    def _checked_cost(cost: int) -> int:
        if not isinstance(cost, int) or isinstance(cost, bool) or cost < 1:
            raise ValueError(f"GraphQL cost must be a positive integer; got {cost!r}")
        return cost

    def _add_resolver(self, spec: ResolverSpec) -> None:
        self._check_mutable()
        self._checked_policy(spec.policy)
        self._checked_cost(spec.cost)
        object_type = self._schema.types.get(spec.type_name)
        if object_type is None:
            raise ResolverError(
                f"no type named {spec.type_name!r} is exposed; a resolver cannot "
                "attach to a model that was not included in `models`"
            )
        self._resolvers.add(spec)
        object_type.fields[spec.field_name] = SchemaField(
            name=spec.field_name,
            type_name=spec.type_name_out,
            non_null=spec.non_null,
            is_list=spec.is_list,
            resolver=spec,
            policy=spec.policy or f"{spec.type_name}.{spec.field_name}",
            cost=spec.cost,
        )
        self._policy_schema = None

    def _add_root(
        self,
        name: str,
        fn: Any,
        returns: str,
        is_list: bool,
        policy: str | None,
        cost: int,
        *,
        mutation: bool,
    ) -> None:
        self._check_mutable()
        self._checked_policy(policy)
        self._checked_cost(cost)
        # No `cost=` on the spec: `ResolverSpec.cost` is read in exactly one
        # place, `_add_resolver`, where it is copied onto the `SchemaField` of a
        # field *on a type*. A root's weight lives on the `RootField` below,
        # which is what `cost.weigh` looks up. Setting both would leave one of
        # them inert, which is the state `cost=` was in across the whole stack
        # until the weigher existed.
        spec = ResolverSpec(
            type_name="Mutation" if mutation else "Query",
            field_name=name,
            fn=fn,
            type_name_out=returns,
            is_list=is_list,
            policy=policy,
        )
        self._resolvers.add_root(spec, mutation=mutation)
        root = RootField(
            name=name,
            type_name=returns,
            is_list=is_list,
            resolver=spec,
            policy=policy or f"{'Mutation' if mutation else 'Query'}.{name}",
            cost=cost,
        )
        target = self._schema.mutations if mutation else self._schema.roots
        target[name] = root
        self._policy_schema = None

    def _check_mutable(self) -> None:
        if self._frozen:
            raise ResolverError(
                "resolvers must be registered before the endpoint serves its "
                "first request; register them at import or startup"
            )

    def validate(self) -> None:
        """Check every resolver dependency resolves, and freeze the schema.

        Called automatically by `router`. A missing or cyclic `requires`
        is a wiring mistake, and finding it on the first request that happens to
        select that field is far too late.
        """
        validate_dependencies(
            self._resolvers,
            {name: set(type_info.fields) for name, type_info in self._schema.types.items()},
        )
        self._ensure_policy_schema()
        self._frozen = True

    def _ensure_policy_schema(self) -> Any:
        """Compile immutable policy names once for native request-local decisions."""
        if self._policy_schema is None:
            policies = (
                tuple(
                    schema_field.policy
                    for object_type in self._schema.types.values()
                    for schema_field in object_type.fields.values()
                )
                + tuple(root.policy for root in self._schema.roots.values())
                + tuple(root.policy for root in self._schema.mutations.values())
            )
            self._policy_schema = _core.graphql_policy_schema(policies, policy_resource)
        return self._policy_schema

    def declared_actions(self) -> dict[str, tuple[str, ...]]:
        """Resource type -> the Cedar actions this endpoint is gated on.

        The same shape `wreath.authorization.declared_actions` returns for an
        application's routes, and read off the same declarations enforcement
        uses -- every field's policy resource, resolved through
        `policy_resource`, grouped by its entity type.

        GraphQL's half of the shared vocabulary is the mirror image of REST's.
        A route names one action over a resource type whose *rows* it cannot
        enumerate; a GraphQL endpoint names one action -- the `action=` it was
        constructed with -- over resources it can enumerate exactly, because a
        field is declared rather than stored. So the manifest's type-level
        answer is the coarse one here, and `POST {prefix}` with the field names
        as ids is the exact one: it asks `authorize(action, User::"email")`,
        which is character for character the decision the endpoint takes.

        Empty when no authorizer was given, because then nothing is enforced
        and a vocabulary entry would advertise a control that does not exist.
        """
        if self._authorizer is None:
            return {}
        found: dict[str, set[str]] = {}
        policies = (
            *(
                schema_field.policy
                for object_type in self._schema.types.values()
                for schema_field in object_type.fields.values()
            ),
            *(root.policy for root in self._schema.roots.values()),
            *(root.policy for root in self._schema.mutations.values()),
        )
        for policy in policies:
            found.setdefault(policy_resource(policy).type, set()).add(self._action)
        return {name: tuple(sorted(actions)) for name, actions in sorted(found.items())}

    def sdl(self) -> str:
        """The schema in SDL form."""
        return self._schema.sdl()

    def parse(self, source: str) -> Any:
        """Parse and cache `source` under the configured limits.

        Documents past `MAX_CACHED_QUERY_CHARS` are parsed and *not*
        cached: the key is the client's own text, so caching by entry count
        alone let a few large documents hold far more memory than the count
        suggested, and a caller choosing the key is a caller choosing the size.
        """
        if len(source) > MAX_CACHED_QUERY_CHARS:
            return parse(source, self._limits)
        cached = self._cache.get(source)
        if cached is not None:
            return cached.document
        document = parse(source, self._limits)
        self._cache.set(source, _CachedDocument(document))
        return document

    def _prepare(self, source: str, operation_name: str | None) -> Any:
        """Parse and charge one operation, reusing the schema-bound result.

        Only a validated schema may retain the result.  ``run()`` is public and
        can be used before ``router()`` freezes declarations; caching a weight
        there would let a later ``cost=`` declaration make the remembered fact
        stale.  Oversized documents remain deliberately uncached as well.
        """
        if len(source) > MAX_CACHED_QUERY_CHARS:
            document = parse(source, self._limits)
            self._weigh(document, operation_name)
            return document

        cached = self._cache.get(source)
        if cached is None:
            document = parse(source, self._limits)
            cached = _CachedDocument(document)
            self._cache.set(source, cached)
        else:
            document = cached.document

        if not self._frozen:
            self._weigh(document, operation_name)
            return document

        try:
            operation = document.operation(operation_name)
        except KeyError, ValueError:
            # Execution owns the useful error for an unresolved operation.
            return document
        key = operation.name
        if key in cached.weighed:
            return document
        weigh(
            self._schema,
            document,
            operation,
            max_complexity=self._limits.max_complexity,
            max_page_size=self._max_page_size,
        )
        self._cache.set(
            source,
            _CachedDocument(document, cached.weighed | frozenset((key,))),
        )
        return document

    def _weigh(self, document: Any, operation_name: str | None) -> None:
        """Charge the operation's declared costs against `max_complexity`.

        A second pass, because it cannot be a first one: the parser counts
        selections while it reads, and a weight that depends on *which field* a
        name resolves to is not knowable until the schema is. Until this ran,
        `cost=` on `field()`, `query()` and `mutation()` was threaded through
        three layers and read by nothing, so a field that called a payment
        provider counted exactly as much as reading a column.

        An operation this cannot resolve -- a name the document does not define,
        or an unnamed request against a multi-operation document -- is left
        alone. `execute` resolves the same operation moments later and reports
        that properly; refusing it here would answer "no such operation" with
        `complexity`.
        """
        try:
            operation = document.operation(operation_name)
        except KeyError, ValueError:
            return
        weigh(
            self._schema,
            document,
            operation,
            max_complexity=self._limits.max_complexity,
            max_page_size=self._max_page_size,
        )

    async def _run(
        self,
        source: str,
        session: Any,
        *,
        operation_name: str | None,
        variables: dict[str, Any] | None,
        request: Any,
        json_output: bool,
    ) -> dict[str, Any] | bytes:
        from ._json import dumps

        def result(body: dict[str, Any]) -> dict[str, Any] | bytes:
            return dumps(body) if json_output else body

        try:
            document = self._prepare(source, operation_name)
        except GraphQLSyntaxError as error:
            return result({"errors": [{"message": str(error), "extensions": {"code": error.code}}]})
        try:
            executor = execute_json if json_output else execute
            data = await executor(
                self._schema,
                document,
                session,
                operation_name=operation_name,
                variables=variables,
                authorizer=self._authorizer,
                request=request,
                max_page_size=self._max_page_size,
                on_denied=self._on_denied,
                action=self._action,
                policy_schema=self._ensure_policy_schema(),
            )
        except ExecutionError as error:
            body: dict[str, Any] = {"message": str(error)}
            if error.path:
                body["path"] = list(error.path)
            return result({"data": None, "errors": [body]})
        except Exception:  # noqa: BLE001 - a resolver, not the transport
            # A resolver raising something that is not an `ExecutionError` used
            # to escape into the ASGI layer and answer 500, which every GraphQL
            # client reads as a network failure rather than a field error. The
            # message is deliberately generic: a resolver's exception text is
            # server-side detail (see `wreath.crud._unprocessable`).
            self.resolver_errors += 1
            return result(
                {
                    "data": None,
                    "errors": [
                        {
                            "message": "the resolver failed",
                            "extensions": {
                                "code": "RESOLVER_ERROR",
                            },
                        }
                    ],
                }
            )
        if json_output:
            return data
        return {"data": data}

    async def run(
        self,
        source: str,
        session: Any,
        *,
        operation_name: str | None = None,
        variables: dict[str, Any] | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        """Parse and execute `source`, returning a GraphQL response body."""
        body = await self._run(
            source,
            session,
            operation_name=operation_name,
            variables=variables,
            request=request,
            json_output=False,
        )
        if not isinstance(body, dict):
            raise TypeError("GraphQL.run() must produce a response object")
        return body

    def router(
        self,
        path: str = "/graphql",
        *,
        session_factory: Any = None,
    ) -> Router:
        """A router serving POST `path` (and GET for the SDL, if enabled).

        `session_factory(request)` supplies the ORM session. Without one, a
        read session is opened per request from the registry and closed after.
        """
        self.validate()
        router = Router()
        graphql = self

        @router.post(path)
        async def _run(request: Request) -> Response:
            # A GraphQL POST must be JSON. Accepting any body that happens to
            # parse made this endpoint reachable by a cross-origin <form>: a
            # `text/plain` POST is a "simple request", so the browser sends it
            # (with cookies) without a preflight the CORS policy could refuse.
            # Requiring a content-type that forms cannot produce is what the
            # GraphQL-over-HTTP spec asks for, and it is the whole defence for a
            # cookie-authenticated schema.
            content_type = (request.header("content-type") or "").split(";")[0].strip()
            if content_type not in _ACCEPTED_CONTENT_TYPES:
                return JSONResponse(
                    {
                        "errors": [
                            {
                                "message": (
                                    "a GraphQL request must be sent as "
                                    f"{' or '.join(sorted(_ACCEPTED_CONTENT_TYPES))}"
                                )
                            }
                        ]
                    },
                    status=415,
                )
            try:
                payload = await request.json()
            except ValueError:
                return JSONResponse(
                    {"errors": [{"message": "request body is not valid JSON"}]},
                    status=400,
                )
            if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
                return JSONResponse(
                    {"errors": [{"message": "expected a JSON object with a `query` string"}]},
                    status=400,
                )
            variables = payload.get("variables")
            if variables is not None and not isinstance(variables, dict):
                return JSONResponse(
                    {"errors": [{"message": "`variables` must be an object"}]},
                    status=400,
                )
            operation_name = payload.get("operationName")
            if not isinstance(operation_name, str):
                operation_name = None

            # A document that mutates needs the write workload; a read session
            # would send the mutation to a replica.
            session, close = await graphql._session(
                request,
                session_factory,
                mutating=graphql._is_mutation(
                    payload["query"],
                    operation_name=operation_name,
                ),
            )
            try:
                body = await graphql._run(
                    payload["query"],
                    session,
                    operation_name=operation_name,
                    variables=variables,
                    request=request,
                    json_output=True,
                )
            finally:
                if close is not None:
                    await close()
            # GraphQL answers 200 with an `errors` array; a transport status
            # would be read as a network failure by every client library.
            if not isinstance(body, bytes):
                raise TypeError("GraphQL HTTP execution must produce JSON bytes")
            return Response(body, media_type=b"application/json")

        # One route fronts every field in the schema, so the route's own
        # requirement says nothing about what this endpoint enforces.
        # `permissions_router` and `wreath typegen` read the field policies
        # back through this name rather than being handed a copy of them.
        setattr(_run, SURFACE_ACTIONS, self.declared_actions)

        if self._introspection:

            @router.get(path)
            async def _sdl(request: Request) -> Any:
                from .response import Response

                return Response(
                    graphql.sdl().encode("utf-8"), media_type=b"text/plain; charset=utf-8"
                )

        return router

    def _is_mutation(self, source: str, *, operation_name: str | None = None) -> bool:
        """Whether `source` parses to a mutation. False for anything unparseable."""
        try:
            document = self.parse(source)
        except GraphQLSyntaxError:
            return False
        try:
            return document.operation(operation_name).operation == "mutation"
        except KeyError, ValueError:
            # `Document.operation` raises `KeyError` for a name it does not hold
            # and `ValueError` when the document has no unambiguous operation.
            # Neither is a mutation, and a document that cannot name one will be
            # refused by execution anyway -- this only picks the session.
            return False

    async def _session(
        self, request: Any, session_factory: Any, *, mutating: bool = False
    ) -> tuple[Any, Any]:
        """The session for one request, and how to release it (or None).

        A factory may return a session directly or an async context manager;
        both are common, and which one the caller chose is not something the
        endpoint should care about.

        `mutating` selects the write workload. Without it every request --
        mutations included -- opened a *read* session, so a registered mutation
        ran against whatever the read pool points at, which on a replica is a
        failed write and on a single database is an invisible non-issue until
        the day a replica is added.
        """
        if session_factory is not None:
            supplied: Any = session_factory(request)
            if hasattr(supplied, "__aenter__"):
                opened = await supplied.__aenter__()
                return opened, lambda: supplied.__aexit__(None, None, None)
            return supplied, None
        from .orm.session import Session

        # No factory: one session per request, closed when it is done.
        session = Session(self._registry, "write" if mutating else "read")
        return session, session.close
