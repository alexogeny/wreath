# Prescriptive plan: middleware, authentication, RBAC, Cedar, and framework ergonomics

Status: ready for implementation

## Goal

Add application-layer features users expect from Sanic, FastAPI, BlackSheep, and similar frameworks without sacrificing Neo's dependency-free core or request hot path. Compile middleware and authorization when routes are finalized; do not assemble or introspect them repeatedly per request. Public routes with no middleware or authorization must retain a direct fast path.

This plan covers middleware, explicit state, lifespan hooks, structured errors, route groups, authentication decorators, RBAC, optional Cedar authorization, common security middleware, background tasks, file/static responses, and signed-cookie sessions.

## Fixed constraints

- Target CPython 3.14 with no mandatory third-party runtime dependencies.
- Preserve Neo as a conforming ASGI application on any ASGI server.
- Do not add `current_request`, `current_app`, thread-local state, or global identity.
- Application state belongs to a `Neo`; request state belongs to a `Request`.
- Compile middleware, route metadata, adapters, and auth guards at startup/route compilation.
- Empty middleware/auth configuration must not allocate or traverse a chain per request.
- Authentication and authorization remain separate; declared authorization fails closed.
- Never implement JWT verification, password hashing, or cryptography ad hoc.
- Never implement the Cedar language/parser/evaluator in Neo. Inject an optional engine.
- Never trust forwarding headers without explicit trusted-proxy configuration.
- Run benchmarks only after correctness checks pass.

## Target API

```python
from neo import Neo
from neo.auth import authenticated, permissions, roles
from neo.middleware import CORS, RequestID, SecurityHeaders, TrustedHost

app = Neo()
app.add_middleware(RequestID())
app.add_middleware(TrustedHost({"api.example.com"}))
app.add_middleware(CORS(allow_origins={"https://app.example.com"}))
app.add_middleware(SecurityHeaders())
app.configure_auth(backend=my_backend, authorizer=my_authorizer)

admin = app.group("/admin", name="admin")

@admin.get("/users/{user_id}", name="read-user")
@authenticated()
@roles("admin", "support", mode="any")
@permissions("users:read")
async def read_user(request):
    return {"viewer": request.identity.id, "user_id": request.path_params["user_id"]}
```

Cedar remains explicit:

```python
from neo.auth.cedar import CedarAuthorizer

authorizer = CedarAuthorizer(
    engine=engine,
    principal=map_principal,
    action=map_action,
    resource=map_resource,
    entities=load_entities,
    context=build_context,
)

@app.get("/documents/{document_id}", name="documents:read")
@authorize(action="Document::Action::read", resource=document_resource)
async def read_document(request): ...
```

## Core contracts

### State

Create `src/neo/state.py`:

```python
class State:
    __slots__ = ("_values",)
    def __getattr__(self, name: str): ...
    def __setattr__(self, name: str, value): ...
    def __delattr__(self, name: str): ...
    def get(self, name: str, default=None): ...
    def require(self, name: str): ...
```

Expose `app.state` and lazily allocated `request.state`. Do not use ASGI scope as the primary ownership model.

### HTTP exceptions

Create `src/neo/exceptions.py` with `HTTPException(status, detail, headers)` and subclasses for 400, 401, 403, 404, 405, 409, 422, and 429. `Unauthorized` supports `WWW-Authenticate`. Missing/invalid credentials use 401; authenticated denials use 403.

Add `app.add_exception_handler(type, handler)`, `app.add_status_handler(status, handler)`, and `@app.exception_handler(type)`. Resolve exact type, nearest registered superclass, status handler, default HTTP renderer, then existing 500 behavior. Compile lookup tables. Never expose traceback details unless debug is enabled.

### Middleware

Create `src/neo/middleware/base.py`:

```python
ResponseValue = Response | StreamingResponse | dict | str | bytes | None
CallNext = Callable[[Request], Awaitable[ResponseValue]]

class Middleware(Protocol):
    async def __call__(self, request: Request, call_next: CallNext) -> ResponseValue: ...
```

Support separately:

1. Neo HTTP middleware over `Request` and response-compatible values.
2. Standard ASGI middleware wrapping scope/receive/send.

Registration:

```python
app.add_middleware(middleware, *, priority: int = 0)
app.add_asgi_middleware(factory, *args, **kwargs)
```

Order by priority then registration order. Request flow follows order; response flow unwinds in reverse. Route/group middleware runs inside app middleware. Exception conversion surrounds endpoint, auth, and Neo middleware execution.

### Route definitions and groups

Replace handler-only registration with:

```python
@dataclass(slots=True)
class RouteDefinition:
    path: str
    methods: tuple[str, ...]
    endpoint: Handler
    name: str | None
    middleware: tuple[Middleware, ...]
    auth: AuthRequirement | None
    tags: tuple[str, ...]
    include_in_schema: bool
    metadata: Mapping[str, object]
```

Add a registration-only `RouteGroup` carrying prefix, name, middleware, auth defaults, and tags. Flatten groups into route definitions; do not traverse parents per request.

### Authentication models

Create `src/neo/auth/models.py`:

```python
@dataclass(frozen=True, slots=True)
class Identity:
    id: str
    type: str = "User"
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    claims: Mapping[str, object] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class Credentials:
    scheme: str
    value: str

@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str | None = None
    diagnostics: tuple[str, ...] = ()
```

Never place raw tokens, passwords, signing keys, or other secrets in claims. Add read-only request properties `identity`, `credentials`, and `authenticated`.

Create backend protocols:

```python
class AuthenticationBackend(Protocol):
    async def authenticate(self, request: Request) -> Identity | None: ...
    def challenge(self, request: Request) -> tuple[bytes, bytes] | None: ...

class AuthorizationProvider(Protocol):
    async def authorize(self, request: Request, requirement: AuthRequirement) -> AuthorizationDecision: ...
```

Provide callback adapters: `BearerTokenBackend(verifier)`, `APIKeyBackend(verifier, header="x-api-key")`, `SessionBackend(loader)`, and `CompositeAuthenticationBackend(backends)`. Neo extracts credentials; user callbacks perform verification and storage access.

### Decorators

Create:

```python
@authenticated()
@anonymous_allowed()
@roles("admin", mode="all" | "any")
@permissions("users:read", mode="all" | "any")
@authorize(action="Document::Action::read", resource=resolver)
```

Decorators attach immutable metadata rather than wrappers. Resolve metadata during `app.compile()`. Support both decorator orders if route definitions retain endpoint references; otherwise support route-outermost order and raise a startup error instead of silently ignoring auth metadata.

Merge rules:

- Explicit route metadata overrides group defaults only where allowed.
- Multiple role/permission decorators combine with logical AND.
- Values inside one decorator use its `mode`.
- `anonymous_allowed()` may override inherited authentication but not explicit route authorization.
- Conflicts fail during compilation.

## Ordered implementation work

### 1. Lock baseline behavior

Add benchmark scenarios for base routes, empty middleware, one/five no-op middleware, authenticated identity, RBAC allow/deny, and CORS preflight. Record raw output without making claims from one run.

### 2. Add state and structured errors

Implement state and HTTP errors first. Test state isolation, exception MRO resolution, preserved headers, 401 challenge behavior, and hidden debug details.

### 3. Add route compilation

Refactor `Neo.route()` to retain `RouteDefinition`. Add `app.compile()`, `app.compiled`, and optional `app.freeze()`.

Compilation must:

1. Validate route names and uniqueness.
2. Resolve group/decorator metadata.
3. Compile endpoint adapters.
4. Compile app/group/route middleware.
5. Compile authentication/authorization guards.
6. Insert final callables into the existing `Router`.
7. Bind `_match`.

Compile during lifespan startup or synchronously on first request if lifespan is disabled. Configuration mutation either marks the app dirty or is rejected after freeze; choose and test one policy. Routes with no middleware/auth compile directly to the endpoint adapter.

### 4. Add middleware

Compile reversed function composition once per configuration revision. Do not build chains or inspect signatures per request. Add `@app.middleware` convenience.

Test deterministic ordering, priorities, short circuits, response replacement, exceptions, nesting, ASGI lifespan visibility, and empty-chain parity.

### 5. Add lifespan hooks

Create `src/neo/lifecycle.py`:

```python
@app.on_startup
async def connect(app): ...
@app.on_shutdown
async def disconnect(app): ...
@app.lifespan
@asynccontextmanager
async def lifespan(app): ...
```

Startup runs in registration order; shutdown reverses it. On partial startup failure, clean up acquired resources only. Store resources on `app.state`. Compile routes before startup completes.

### 6. Add route groups and URL reversing

Implement `group`, `include`, `url_path_for`, and all standard method decorators. Normalize prefixes once, preserve trailing slashes, reject duplicate names, and validate reverse parameters. Group metadata becomes immutable after compilation.

### 7. Add authentication

Public API:

```python
app.configure_auth(
    backend,
    authorizer=None,
    *,
    authenticate_all=False,
)
```

By default, invoke authentication only for routes declaring auth requirements. Execution:

1. Extract credentials.
2. Verify/load immutable identity.
3. Store it on the request.
4. Missing/invalid credentials on a protected route raise `Unauthorized` with challenge.
5. Anonymous routes continue without identity.
6. Backend operational failures do not become anonymous access.

Test missing/malformed/invalid/valid credentials, composite ordering, duplicate Authorization headers, secret-safe errors/reprs, public-route bypass, authenticate-once behavior, and concurrent identity isolation.

### 8. Add RBAC

Create `src/neo/auth/rbac.py`:

```python
@dataclass(frozen=True, slots=True)
class Role:
    name: str
    permissions: frozenset[str]
    inherits: frozenset[str] = frozenset()

class RBACAuthorizer:
    def __init__(self, roles: Iterable[Role]): ...
```

At configuration time reject duplicate roles and inheritance cycles, expand inherited permissions once, and store immutable sets. Unknown roles grant nothing. Permission matching is exact; do not add wildcards without a separate specification.

Evaluation:

- `authenticated()` requires identity.
- Role/permission `any` needs one; `all` needs all.
- Permissions combine identity permissions and compiled role permissions.
- Every declared requirement passes or access is denied.
- Authenticated denial returns 403.

Test inheritance, cycles, unknown roles, exact matching, combined decorators, groups, anonymous overrides, and deterministic denial reasons.

### 9. Add Cedar adapter without implementing Cedar

Create `src/neo/auth/cedar.py`:

```python
class CedarEngine(Protocol):
    def is_authorized(
        self,
        *,
        principal: object,
        action: object,
        resource: object,
        context: Mapping[str, object],
        entities: object,
    ) -> object: ...
```

`CedarAuthorizer` receives explicit principal/action/resource/entity/context mappers. It owns mapping and decision normalization only.

Rules:

- Do not silently convert route names to Cedar entity UIDs.
- Resource mapping may use path parameters.
- Context is allowlisted; never pass the whole request, headers, cookies, scope, or all claims.
- Entity loading may be sync or async.
- Mapper/engine errors deny protected access and report sanitized operational diagnostics.
- Valid authenticated deny returns 403.
- Do not cache decisions by default. A future cache key includes policy revision, principal, action, resource, and relevant context.

Real-engine gate:

1. Evaluate maintained CPython-accessible Cedar engines.
2. Require official semantics, validation, entities, and acceptable security/release posture.
3. Put third-party/Rust-backed engines behind an optional extra or separate integration package.
4. Never add one to base dependencies.
5. If no engine qualifies, ship the protocol/adapter but clearly state Neo does not bundle Cedar evaluation.

Test allow, explicit forbid, default deny, errors, context allowlisting, path resources, async entities, sanitized diagnostics, and cache revision behavior.

### 10. Add authorization audit hooks

Define `AuthorizationAuditSink.record(event)`. Event data includes immutable UTC timestamp, request ID, principal ID/type, action, safe resource ID, decision, provider, policy revision, reason code, and sanitized diagnostics.

Never include credentials, cookies, bodies, or arbitrary claims. Default audit failure to fail-open with an error hook; allow explicit fail-closed configuration for regulated deployments.

### 11. Add built-in middleware

Create modules under `src/neo/middleware/`:

- `CORS`: exact origins, methods/headers, credentials, exposed headers, max age, correct `Vary`; never wildcard origin with credentials.
- `TrustedHost`: exact hosts and explicit wildcard subdomains; parse ports safely and avoid suffix-match bugs.
- `RequestID`: validate incoming length/characters or generate with stdlib randomness; store on request state and response.
- `SecurityHeaders`: conservative opt-ins; HSTS only when enabled and CSP only when configured.
- `Compression`: stdlib gzip, eligible types/statuses, minimum size, `Vary`, corrected length; skip streaming initially.
- `ProxyHeaders`: disabled by default; only configured proxy IP/networks may override scheme/host/client.

Add security-focused tests for every malformed and bypass case.

### 12. Add background tasks and file responses

Create `BackgroundTask` and `BackgroundTasks`; attach them to responses and run after the terminal body. Failures go to an application error hook. Document that these are not durable jobs.

Add `FileResponse` with bounded reads, length, last-modified, optional ETag, conditional GET, and HEAD. Implement range requests only if fully tested. Detect ASGI pathsend extensions with a portable fallback.

### 13. Add signed-cookie sessions

Create middleware using stdlib HMAC-SHA-256, JSON, and base64. Configuration includes rotating keys, cookie name, age, secure, http-only, same-site, path, and domain.

Rules:

- Sign with first key; verify all keys using constant-time comparison.
- Include issued/expiry data.
- Invalid/expired data becomes an empty session with optional tamper hook.
- Enforce conservative size limits.
- Signed is not encrypted; never store secrets.
- Keep server-side stores behind a future `SessionStore` protocol.

### 14. Add static files

Resolve requested paths under a configured root, reject traversal after URL decoding/resolution, disallow symlink escape unless enabled, keep directory listing off, and avoid exposing filesystem paths. Use `FileResponse` and deterministic cache headers.

### 15. Preserve extension seams

Do not prematurely implement these larger subsystems:

- Validation/DI: reserve a compiled endpoint adapter; never inspect annotations per request. Pydantic may be an optional adapter, not mandatory.
- OpenAPI: preserve names, tags, descriptions, visibility, and parameter metadata; generate only after validation contracts stabilize.
- Rate limiting: define providers only after local/distributed requirements; do not present an in-memory limiter as multi-worker safe.
- Observability: expose request/exception/auth decision hooks; OpenTelemetry remains optional.
- Templates: use a future renderer protocol; bundle no template language.
- WebSockets: allow future reuse of identity/auth contracts without claiming current support.

## Performance requirements

Measure separately:

```text
base route
empty middleware registry
one and five no-op middleware
route middleware
public route with auth configured but unused
authenticated route
RBAC allow and deny
Cedar adapter allow and deny
CORS simple request and preflight
```

Required properties:

- Public routes do not invoke auth unless `authenticate_all=True`.
- Empty middleware preserves direct dispatch.
- Chains and decorator metadata compile once per revision.
- Role inheritance compiles once per RBAC revision.

Add scenarios to `benchmarks/scenarios.py` with capability declarations. Mark unimplemented competitor pairs unavailable rather than faking comparisons.

## Expected files

```text
src/neo/app.py
src/neo/request.py
src/neo/response.py
src/neo/routing.py
src/neo/state.py
src/neo/exceptions.py
src/neo/lifecycle.py
src/neo/background.py
src/neo/sessions.py
src/neo/static.py
src/neo/middleware/{__init__,base,cors,trusted_host,request_id,security,compression,proxy_headers}.py
src/neo/auth/{__init__,models,backends,decorators,requirements,rbac,cedar,audit}.py
tests/test_state.py
tests/test_exceptions.py
tests/test_lifecycle.py
tests/test_middleware.py
tests/test_route_groups.py
tests/test_authentication.py
tests/test_rbac.py
tests/test_cedar.py
tests/test_builtin_middleware.py
tests/test_background.py
tests/test_sessions.py
tests/test_static.py
benchmarks/apps.py
benchmarks/scenarios.py
benchmarks/README.md
README.md
docs/auth.md
docs/middleware.md
```

Do not edit generated `src/neo_asgi.egg-info/PKG-INFO` manually.

## Verification sequence

Run focused tests after each work package, then:

```bash
uv run pytest
uv run ruff check .
uv run ty check
```

Only after all correctness checks pass:

```bash
uv sync --group benchmark
uv run python -m benchmarks.run
```

For performance claims, repeat targeted scenarios with larger explicit request counts and retain raw outputs. Do not treat the bundled load generator as publication-grade evidence.

## Completion criteria

- State ownership is explicit and concurrent requests are isolated.
- Lifespan hooks correctly acquire/release resources.
- Structured errors work through endpoints and middleware.
- Empty middleware/auth retains direct compiled dispatch.
- App/group/route middleware ordering is deterministic.
- Groups flatten metadata without runtime parent traversal.
- Decorators compile metadata instead of repeated introspection.
- Missing/invalid credentials return challenged 401; authenticated denial returns 403.
- Public routes bypass auth by default.
- RBAC inheritance is cycle-checked, immutable, exact-match, and deny-by-default.
- Cedar mapping is explicit; no home-grown evaluator or mandatory engine exists.
- Cedar failures fail closed without leaking diagnostics.
- Audit events contain no credentials or bodies.
- Built-in middleware has security bypass tests.
- Background task failures are observable.
- Sessions use rotation, expiry, HMAC, and constant-time verification.
- Static traversal/symlink escapes are covered.
- Full tests, Ruff, and type checks pass.
- Neo remains a plain ASGI application on Uvicorn.
- Benchmarks distinguish base dispatch, middleware, authentication, RBAC, Cedar-adapter, and built-in middleware overhead.
