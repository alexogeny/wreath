# Rename `neo` → `wreath`: migration and public-API plan

Status: **implementing.** Grounded in repository inspection on 2026-07-17.
Preserves all existing capabilities; no behavioural or architectural changes
beyond module relocation, renaming, and public-surface tightening. Docs/README
are explicitly out of scope (handled in a later pass).

### Decisions locked (2026-07-17)

1. **Route decorators:** keep as `Wreath`/`Router` methods; no module-level
   `get/post/…`. Import example revised to `app.get(...)` / `router.*`.
2. **Auth:** **split now** — public `wreath.auth` (authentication) and
   `wreath.authorization` (authz/Cedar/requirements); implementation in
   `wreath._auth/`, both public modules are thin facades.
3. **Distribution name:** `wreath` (import package also `wreath`). Verify PyPI
   availability before first publish.
4. **C rename:** **full** — `neo_*`→`wreath_*`, `NEO_*`→`WREATH_*`,
   `neocore.h`→`wreathcore.h`, plus all module-path/capsule strings, across the
   61 C/H files.

---

## 1. What the repository actually is (grounding corrections)

Inspection overturned several assumptions in the brief:

- **`webpolicy` is not authentication.** `neo/webpolicy.py` is browser
  response/request *policy header* plumbing (origin matching, content-encoding
  negotiation, `Cache-Control` flag parsing, `Vary`/header helpers). It is a
  low-level helper consumed by middleware — not an auth surface.
- **Auth already exists as a proper subpackage.** `neo/auth/` has
  `models.py` (`Identity`, `Credentials`, `AuthorizationDecision`),
  `backends.py` (`AuthenticationBackend`, `AuthorizationProvider`,
  `BearerTokenBackend`), `decorators.py` (`authenticated`, `authorize`,
  `roles`, `permissions`), `cedar.py` (`CedarEngine`, `CedarAuthorizer`),
  `requirements.py` (`AuthRequirement`, `PolicyRequirement`). **344 LOC total.**
- **Middleware already exists as a proper subpackage.** `neo/middleware/`:
  `base` (`Middleware`, `MiddlewareRoute`, `compile_middleware`), `cors`,
  `security`, `compression`, `cache`, `ratelimit`, `request_id`, `timing`,
  `proxy`, plus `CSRFMiddleware`, `SessionMiddleware`, `TrustedHostMiddleware`.
- **`router` vs `routing` is a real public/private split, not an alias.**
  `router.py` = public composition (`Router`, `RouteDefinition`, `.get/.post/…`
  decorators, inclusion into the app). `routing.py` = the low-level `Router`
  with two interchangeable **compiled matcher backends** (bitset/decision).
- **`websocket` vs `ws` is public/private.** `websocket.py` = the public
  connection API (`WebSocket`, `WebSocketDisconnect`). `ws.py` = RFC 6455
  **frame primitives** (`build_frame`, `mask`, `parse_frame`).
- **`cache` vs `snapshot`.** `cache.py` = HTTP **`Cache-Control` policy value
  objects** (`CacheControl`, `CachePolicy`, `PRIVATE_NO_STORE`) — headers, not a
  cache. `snapshot.py` = a **real application cache** (`SnapshotCache`:
  keyed, bounded `max_entries`, `get`/`require`/`get_many`/`replace`/`refresh`,
  generations, atomic snapshot publication).
- **`compression`** has a reusable **codec** (`GzipCompressor`, `gzip_compress`)
  plus negotiation (`select_content_encoding` in `webpolicy`), distinct from the
  single `CompressionMiddleware` — so a public codec module is justified.
- **`config` vs `state` are already distinct.** `config.py` = env/dotenv
  (`Environment`, `load_env`, `parse_dotenv`, `read_osenv`). `state.py` =
  `State` (runtime app/request state). Typed server settings (`ServerConfig`)
  live in `server.py`.
- **`orm → postgres` direction is already clean.** `orm/session.py` imports
  `Workload`, `_WORKLOADS` from `postgres`; `postgres.py` imports **nothing**
  from `orm`. (Minor: `_WORKLOADS` is a private cross-module import — see §7.)
- **telemetry / inspector / recording / replay do not exist.** Only internal
  `_native/observability.c` + `_pure/observability.py` and `_devtools/` CLIs.
  Per your note, these ship as **empty "not implemented" scaffolds**.

---

## 2. Target public module map

Legend — Action: **keep** (same file, renamed pkg), **rename**, **move**,
**private** (internalise), **scaffold** (new empty stub), **merge**.

| Public module | Source today | Action | Notes |
|---|---|---|---|
| `wreath.app` | `neo/app.py` | keep | `Neo` → **`Wreath`** (class + all refs). |
| `wreath.router` | `neo/router.py` | keep | Public composition + `.get/.post/…`. Only public routing module. |
| `wreath.request` | `neo/request.py` | keep | `Request`, `FormData`, `UploadedFile`, `RequestLimits`. |
| `wreath.response` | `neo/response.py` | keep | `Response`, `JSONResponse`, `HTMLResponse`, `TextResponse`, `StreamingResponse`, `FileResponse`, `RedirectResponse`, `ProblemResponse`, `ProblemDetail`, `PreparedResponse`. |
| `wreath.binding` | `neo/binding.py` | keep | Binding + validation + DI (`Depends`) unified — no separate DI module (§7). |
| `wreath.middleware` | `neo/middleware/` | keep | All built-ins + protocol. `CompressionMiddleware`/`CacheControlMiddleware` live here. |
| `wreath.auth` | `neo/auth/` | keep+merge | Authentication **and** authorization (344 LOC, shared `Identity`/decorators). See §6. |
| `wreath.websocket` | `neo/websocket.py` | keep | Public connection API. |
| `wreath.postgres` | `neo/postgres.py` | keep | Connections, pools, transactions, codecs, results. No ORM concepts. |
| `wreath.orm` | `neo/orm/` | keep | Models, fields, relations, query, session. Depends on `postgres` only. |
| `wreath.server` | `neo/server.py` | keep | `Wreath`-agnostic native server + `ServerConfig`/`TLSConfig`/`run`/`serve`. |
| `wreath.testing` | `neo/testing.py` | keep | `TestClient`, `TestResponse`, `WebSocketTestSession`. |
| `wreath.staticfiles` | `neo/staticfiles.py` | keep | `StaticFiles`. |
| `wreath.config` | `neo/config.py` | keep | Env/dotenv + typed settings. |
| `wreath.state` | `neo/state.py` | keep | `State`. Not interchangeable with config. |
| `wreath.openapi` | `neo/openapi.py` | keep | `generate_openapi`, `docs_page`. |
| `wreath.cache` | `neo/snapshot.py` | **rename** | The real application cache (`SnapshotCache`). |
| `wreath.cache_control` | `neo/cache.py` | **rename** | HTTP `Cache-Control` policy objects (headers, not a cache). |
| `wreath.compression` | `neo/compression.py` | keep | Reusable codec + negotiation; middleware stays in `wreath.middleware`. |
| `wreath.background` | `neo/background.py` | keep | `BackgroundTask(s)`. (Not in brief; preserved.) |
| `wreath.http_client` | `neo/http_client.py` | keep | Outbound client. (Not in brief; preserved, literal name.) |
| `wreath.webhooks` | `neo/webhooks.py` | keep | Signed inbound/outbound webhooks. (Not in brief; preserved.) |
| `wreath.templates` | `neo/templates.py` | keep | Server-side templates. (Not in brief; preserved.) |
| `wreath.exceptions` | `neo/exceptions.py` | keep | `HTTPException` + typed subclasses. (Not in brief; preserved.) |
| `wreath.typegen` | `neo/typegen/` | keep | Client type generation (TS/fetch/React-Query). (Not in brief; preserved.) |
| `wreath.telemetry` | — | **scaffold** | Empty; `raise NotImplementedError`. Internals exist in `_native/observability.c`. |
| `wreath.inspector` | — | **scaffold** | Empty. |
| `wreath.recording` | — | **scaffold** | Empty. |
| `wreath.replay` | — | **scaffold** | Empty. |
| `wreath.migrations` | — | **scaffold** | Empty. DB migration generator + runner; future work over `wreath.orm`/`wreath.postgres`. |

### Internalised (private) modules

| New private name | Source today | Why private |
|---|---|---|
| `wreath._native` | `neo/_native/` | Compiled accelerators; never a public boundary. |
| `wreath._pure` | `neo/_pure/` | Pure-Python fallback twins. |
| `wreath._routing` | `neo/routing.py` | Compiled matcher backends behind `wreath.router`. |
| `wreath._websocket` | `neo/ws.py` | RFC 6455 frame primitives behind `wreath.websocket`. |
| `wreath._webpolicy` | `neo/webpolicy.py` | Browser policy header helpers behind middleware/response. **Retires the `webpolicy` public name.** |
| `wreath._codecs` | `neo/codecs.py` | Percent/query/cookie byte codecs. |
| `wreath._headers` | `neo/headers.py` | Header-list helpers. |
| `wreath._http` | `neo/http.py` | HTTP/1 request-head parse. |
| `wreath._multipart` | `neo/multipart.py` | Body parsing behind `wreath.request`. |
| `wreath._json` | `neo/json.py` | JSON codec behind request/response. |
| `wreath._devtools` | `neo/_devtools/` | Lint/trace/task CLIs. |
| `wreath._client_codec`, `wreath._cli`, `wreath._devserver` | same names | Already private; keep. |

> Low-level matchers, compiled route programs, tries, bitsets, and dispatch
> internals (`_native/dtrouter.c`, `dtbitset.c`, `router.c`, `routing.py`)
> remain private — satisfies the router boundary rule.

---

## 3. Target top-level `wreath/__init__.py` (deliberately small)

Today's `__init__` re-exports ~50 names (all of webhooks, http_client, every
response subtype, background, cache-control). That is the "entire framework"
re-export the brief warns against. Trim to the common set:

```python
from .app import Wreath
from .router import Router
from .request import Request
from .response import Response, JSONResponse
from .binding import Depends
# get, post, put, patch, delete, websocket — see §5 (open decision).
```

**Removed from top-level** (still available from their obvious modules):
`BackgroundTask(s)` → `wreath.background`; all `Client*`/`HTTPClient` →
`wreath.http_client`; all `Webhook*` → `wreath.webhooks`; `CacheControl` →
`wreath.cache_control`; `FormData`/`UploadedFile`/`RequestLimits` →
`wreath.request`; `HTMLResponse`/`TextResponse`/`StreamingResponse`/
`FileResponse`/`RedirectResponse`/`ProblemResponse`/`ProblemDetail`/
`PreparedResponse` → `wreath.response`; `WebSocket`/`WebSocketDisconnect` →
`wreath.websocket`.

Matches the brief's intended imports, e.g. `from wreath.response import
ProblemResponse`, `from wreath.middleware import CORSMiddleware`, `from
wreath.postgres import Pool`, `from wreath.testing import TestClient`.

---

## 4. Boundary decisions (resolved)

- **Router:** `wreath.router` is the sole public routing module; `routing.py`
  becomes `wreath._routing`. No public matcher/trie/bitset surface.
- **Auth vs authorization:** merge into a single public `wreath.auth`
  (authorization stays inside it — 344 LOC, shared `Identity` and dual-purpose
  decorators justify one module). `webpolicy` is **not** kept public. A future
  `wreath.authorization` split is a clean seam once RBAC/Cedar grows
  (`docs/plans/middleware-auth-rbac-cedar-comforts.md`). See §6 for the split
  option if you'd rather do it now.
- **postgres/orm:** direction already correct and preserved — `wreath.orm →
  wreath.postgres`, never the reverse. `postgres` exposes no ORM concepts.
- **config/state:** kept distinct; no interchangeable API.
- **cache:** real cache → `wreath.cache` (`SnapshotCache`); header policies →
  `wreath.cache_control` (per the brief's rename option).
- **compression:** public codec module retained (`GzipCompressor`,
  `gzip_compress`, negotiation); the middleware stays under `wreath.middleware`.
- **binding:** kept unified; no separate DI/container/resolver modules (no
  independent extension surface found).

---

## 5. Open decision — top-level `get/post/…/websocket`

The brief's example imports module-level `get, post, put, patch, delete,
websocket`. **These do not exist today** — routing decorators are methods on
`Wreath` and `Router` (`app.get(...)`, `router.post(...)`). Adding bare
module-level decorators means either global registry state or a deferred-
registration mechanism — a **new architectural surface**, which the brief also
says to avoid.

Recommendation: **keep decorators as `Wreath`/`Router` methods** and revise the
example to `app.get(...)` / `router.get(...)`. If you want module-level
decorators, treat it as a separate, explicit feature (a `Router` you decorate
then `app.include()` is the lowest-risk shape and already works today).

---

## 6. Auth: merge now, split later (with the split option)

Recommended now — single `wreath.auth` re-exporting the current `auth/` package
(`Identity`, `Credentials`, `AuthenticationBackend`, `BearerTokenBackend`,
`AuthorizationDecision`, `AuthorizationProvider`, `CedarEngine`,
`CedarAuthorizer`, `authenticated`, `authorize`, `roles`, `permissions`,
`AuthRequirement`). `PolicyRequirement`/`AuthRequirement` are the nearest thing
to the brief's `Policy` — do **not** invent a `Policy` class; expose the
existing types.

If you prefer the two-module layout immediately (both names are already
file-separated, so cost is low):
- `wreath.auth` ← `models` + `backends`(authn) + `decorators.authenticated`
- `wreath.authorization` ← `cedar` + `requirements` + `backends`
  (`AuthorizationProvider`) + `decorators.authorize/roles/permissions`

Implementation would live in `wreath._auth/`, with both public modules as thin
facades.

---

## 7. Minor cleanups surfaced (do, but keep scoped)

- `orm` imports `_WORKLOADS` (private) from `postgres`. When `postgres` is
  public, promote the workload registry to a supported name or move the shared
  constant to a small internal module both import. Low urgency.
- `neo/json.py`, `codecs.py`, `headers.py`, `http.py`, `multipart.py` currently
  have public `__all__` but are implementation detail — internalising them (§2)
  is the tightening, not a capability loss (request/response cover the surface).

Do **not** expand scope into: splitting binding into DI/containers, renaming C
symbols wholesale (see §8), or reworking the matcher backends.

---

## 8. Migration mechanics (scale + order)

Measured surface:

- Python: **5** `src/` files use absolute `import neo`/`from neo` (the rest are
  relative `from .` and unaffected by the directory rename); **~99** test files
  and **~39** `benchmarks/` files import `neo`.
- C/H: **61** files reference `neo._native` / `neocore` / `NEO_` / `neo_`.
- `pyproject.toml`: **15** `[project.scripts]`/entry strings contain `neo`;
  `name = "neo-asgi"`; `[tool.setuptools]` uses `package-dir = {"" = "src"}`.
- `setup.py`: **4–5** `Extension("neo._native._…")` declarations.

Ordered steps (each independently testable with `uv run pytest`):

1. **Package dir:** `git mv src/neo src/wreath`.
2. **`pyproject.toml`:** `name` (`neo-asgi` → decide: `wreath` or `wreath-asgi`);
   all 15 script entries (`neo` → `wreath`, `neo-*` → `wreath-*`, module paths
   `neo.…` → `wreath.…`). `packages.find` auto-discovers `wreath`.
3. **`setup.py`:** rename the `Extension` targets `neo._native._*` →
   `wreath._native._*`; docstring.
4. **C module-path strings (highest risk):** the load-bearing ones are the
   **capsule/type name strings**, not the C identifiers —
   `NEO_CORE_CAPI_NAME`, `PyImport_ImportModule("neo._native._core")`,
   `PyCapsule_Import`/`PyCapsule_New` names, and type names like
   `"neo._native._core.BitsetRouteTable"`, `"…RouteTable"`, `"…TokenBucket"`,
   `"…_DNodeRef"`, `"…_C_API"`. These must all flip to `wreath…` **consistently
   between provider and importer** or the extension import fails silently at
   load. The `neo_`-prefixed C functions, `NEO_*` macros, and `neocore.h` are
   **internal identifiers** — renaming them is cosmetic and can be a separate
   mechanical pass or deferred to reduce churn/risk.
5. **Python internal imports:** fix the 5 absolute-import `src/` files; relative
   imports are unaffected.
6. **Module relocations (§2):** `routing→_routing`, `ws→_websocket`,
   `webpolicy→_webpolicy`, `snapshot→cache`, `cache→cache_control`,
   `{json,codecs,headers,http,multipart}→_-prefixed`. Update internal relative
   imports to each.
7. **`app.py`:** `Neo` → `Wreath` (class, `__slots__` refs, docstrings, any
   `self`-type hints, `_devtools/sample_app.py`).
8. **`wreath/__init__.py`:** the small surface in §3.
9. **Scaffolds:** create `telemetry.py`, `inspector.py`, `recording.py`,
   `replay.py`, `migrations.py` — module docstring + `raise NotImplementedError`
   (or `__all__ = []`), no public types yet. `migrations` is the DB migration
   generator/runner surface, to be built over `orm`/`postgres`.
10. **Rebuild + test:** `uv sync --frozen` (recompiles extensions under the new
    paths), then `uv run pytest`, `uv run ruff check .`, `uv run ty check`.
11. **Tests (~99) + benchmarks (~39):** mechanical `neo` → `wreath`, plus fix
    references to relocated modules (`neo.routing.Router` → `wreath.router` or
    `wreath._routing` for backend-specific tests; `neo.ws` → `wreath._websocket`;
    `neo.webpolicy` → `wreath._webpolicy`; `neo.cache.CacheControl` →
    `wreath.cache_control`; `neo.snapshot.SnapshotCache` → `wreath.cache`).
12. **`neo_asgi.egg-info/`** regenerates on build — no manual edit.

### Deprecation strategy

The project is **0.1.0, unreleased, not on PyPI, no git commits** — a **hard
cut** (no `neo` compatibility shim) is appropriate and cleanest. Retire
`routing`/`ws`/`webpolicy` by internalising, not by leaving deprecated public
aliases. Add a transitional `neo` re-export shim **only** if you've already
shared the current name with anyone.

### Explicitly deferred (per "docs overhaul later")

`README.md`, `mkdocs.yml` (`site_name`, `repo_url`), `docs/**` prose, `llms.txt`,
and the `memory/` notes. **Operational** references need updating with the rename
even though prose docs are deferred: `AGENTS.md` and `repo-map.md` cite
`uv run neo-*` command names that change in step 2 — flag for a mechanical pass.

---

## 9. Non-goals / risks

- **Non-goals:** behaviour changes, matcher-backend rework, DI decomposition,
  wholesale C-symbol renames, adding module-level route decorators.
- **Primary risk:** the C capsule/type-name strings (step 4) — a partial rename
  imports cleanly but fails at capsule resolution. Grep-audit provider/consumer
  pairs before building.
- **Secondary risk:** test/benchmark references to now-private modules; the
  compiler won't catch these — a full `uv run pytest -m ''` (network/fuzz/perf
  included) plus a `grep -rE '\bneo\b'` sweep is the backstop.
