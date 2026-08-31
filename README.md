<div align="center">

# Wreath

**The Python web stack for the point where “just add a package” stops working.**

[![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-2f855a?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![ASGI](https://img.shields.io/badge/ASGI-any_server-7c3aed?style=flat-square)](https://asgi.readthedocs.io/)
![HTTP 1.1, 2, 3](https://img.shields.io/badge/HTTP-1.1%20%7C%202%20%7C%203-0891b2?style=flat-square)
![Runtime dependencies: zero](https://img.shields.io/badge/runtime_dependencies-zero-16a34a?style=flat-square)
[![License: MPL-2.0](https://img.shields.io/badge/license-MPL--2.0-64748b?style=flat-square)](https://github.com/alexogeny/wreath/blob/main/LICENSE)

A Python 3.14-first ASGI framework with its own PostgreSQL stack, durable work,
identity, policy, observability and native HTTP server. One coherent package;
no mandatory runtime dependencies.

[Documentation](https://alexogeny.github.io/wreath/) ·
[Build a serious API](https://alexogeny.github.io/wreath/stories/serious-api.html) ·
[Issues](https://github.com/alexogeny/wreath/issues)

</div>

---

## Start with the application, not the integration backlog

A serious service needs validation, identity, PostgreSQL, policy, background work,
observability and somewhere to run. In Wreath these are declarations over one
application lifecycle, not middleware assembled around several competing request
models.

This route gets compiled binding, bounded fields, native JSON, replay-safe writes,
request IDs, security headers and an OpenAPI operation from the same declarations:

```python
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Body, Field
from wreath.policy import HttpPolicy, IdempotencyPolicy
from wreath.policy.request_id import RequestIdPolicy
from wreath.policy.security import SecurityHeadersPolicy


@dataclass
class RunRequest:
    prompt: Annotated[str, Field(min_length=3, max_length=2_000)]
    model: Annotated[str, Field(min_length=2, max_length=80)]


app = Wreath(
    http_policy=HttpPolicy(
        idempotency=IdempotencyPolicy(),
        request_id=RequestIdPolicy(),
        security_headers=SecurityHeadersPolicy(),
    )
)


@app.post("/runs")
async def create_run(
    request: Request,
    command: Annotated[RunRequest, Body()],
) -> dict:
    return {"state": "queued", "model": command.model, "prompt": command.prompt}


app.enable_api_docs(environments=("development",), try_it_out=True)
```

```bash
uv add wreath
uv run wreath dev app:app
```

The framework stays ordinary ASGI, so `uvicorn app:app` remains valid. The native
server is available when you want Wreath's complete HTTP, telemetry and recording
path.

## Put the rest of HTTP to work

Most frameworks stop at familiar methods and response headers. Wreath turns newer
HTTP standards into application-level tools that can simplify an API well beyond
its transport layer:

- **[QUERY](https://www.rfc-editor.org/rfc/rfc10008.html) — rich searches that
  stay safe and idempotent.** Declare
  `@app.query("/search")` when filters are too large or structured for a URL. The
  request can carry a typed body without pretending to create a resource, while
  clients, caches and retry machinery retain the semantics of a read.
- **[API Catalog](https://www.rfc-editor.org/rfc/rfc9727.html) — an API that can
  introduce itself.**
  `app.enable_api_catalog()` publishes the API, its OpenAPI document and its docs
  at `/.well-known/api-catalog`, giving tools a standard discovery point instead
  of another URL they have to be told about.
- **[Link-Template](https://www.rfc-editor.org/rfc/rfc9652.html) — relationships
  without expanding one link per object.** Responses can publish validated RFC
  6570 templates for collections and related resources; Unicode attributes use
  RFC 9651 Display Strings, and the wire value is compiled once at declaration.
- **[OAuth browser BFF](https://www.rfc-editor.org/rfc/rfc10017.html) — use OAuth
  without giving JavaScript a token.** `bff_router()` keeps access and refresh
  tokens in a revocable server-side session, then exposes fixed-origin API routes
  guarded by a preflight-forcing CSRF header. Browser cookies and credentials are
  stripped before forwarding; resource-server cookies are stripped on the way
  back.
- **[OAuth step-up](https://www.rfc-editor.org/rfc/rfc9470.html) — ask for a
  fresher or stronger login in a form clients understand.** `oauth_step_up()`
  evaluates `auth_time` and `acr`, then returns the standard Bearer challenge a
  client can carry into reauthorization.
- **[Content-Digest and Repr-Digest](https://www.rfc-editor.org/rfc/rfc9530.html)
  — integrity that crosses HTTP hops.** Requests can verify the strongest
  supported digest before using a body, while responses can publish content or
  complete-representation digests and negotiate the peer's preferred algorithm.
- **[Deprecation](https://www.rfc-editor.org/rfc/rfc9745.html) and
  [Sunset](https://www.rfc-editor.org/rfc/rfc8594.html) — migrations clients can
  automate.** Put
  `deprecated_at`, `sunset_at` and `deprecation_link` on a route. Wreath emits the
  protocol headers and marks the OpenAPI operation from the same declaration, so
  a retirement date cannot quietly drift away from the contract.
- **[Incremental responses](https://www.rfc-editor.org/rfc/rfc10036.html) —
  streams that proxies know are live.**
  `StreamingResponse` and server-sent events emit the `Incremental` signal, so a
  conforming intermediary can forward each part instead of buffering the whole
  response and turning a live feed into a delayed download.
- **Cache coordination — one declaration from database write to CDN purge.**
  Targeted RFC 9213 `CDN-Cache-Control` can differ from browser freshness;
  `@cached(...)` can expose
  [Cache-Status](https://www.rfc-editor.org/rfc/rfc9211.html), invalidate on
  committed model writes and emit signed cache tags plus
  [Cache-Groups](https://www.rfc-editor.org/rfc/rfc9875.html). Local freshness,
  diagnostics and shared-cache invalidation describe the same dependency graph.
- **[Proxy-Status](https://www.rfc-editor.org/rfc/rfc9209.html) — failure context
  that survives the proxy hop.** Wreath's proxy surfaces a structured account of
  connection, DNS, TLS and upstream failures, so an application operator gets
  more than an anonymous 502.

These are ordinary interoperable HTTP fields and methods, not private client
conventions. Use them with Wreath's native server or any ASGI deployment whose
intermediaries support the corresponding standard.

## Start where your system gets hard

The documentation builds nine complete systems around the pressure that usually
forces a second stack:

| The pressure | Build the system |
|---|---|
| prove the young framework has the serious surfaces already | [A secure product API with users, policy, PostgreSQL, jobs and measurements](https://alexogeny.github.io/wreath/stories/serious-api.html) |
| coordinate chargers, vehicles and microgrids under contention | [A live energy depot](https://alexogeny.github.io/wreath/stories/energy-depot.html) |
| assign durable agent work across every computer on an account | [A personal device fleet](https://alexogeny.github.io/wreath/stories/agent-fleet.html) |
| run signed hooks, schedules and user code durably | [An automation backplane](https://alexogeny.github.io/wreath/stories/automation-backplane.html) |
| give models useful tools without creating a weaker backend | [A governed MCP control room](https://alexogeny.github.io/wreath/stories/mcp-control-room.html) |
| make SAML, SCIM, support access and deletion share one tenant boundary | [An enterprise control plane](https://alexogeny.github.io/wreath/stories/enterprise.html) |
| analyse irregular series across late data and daylight saving | [A time-series laboratory](https://alexogeny.github.io/wreath/stories/time-series-lab.html) |
| keep scarce inventory correct through retries and webhook redelivery | [A noon-drop storefront](https://alexogeny.github.io/wreath/stories/noon-drop.html) |
| resume sync and uploads through unreliable field networks | [An offline operations service](https://alexogeny.github.io/wreath/stories/field-operations.html) |

Underneath those stories are the conventional guides: [HTTP and OpenAPI](https://alexogeny.github.io/wreath/guides/http-api.html),
[data and migrations](https://alexogeny.github.io/wreath/guides/migration-workflow.html),
[CLI tasks](https://alexogeny.github.io/wreath/guides/cli.html), and the
[production runbook](https://alexogeny.github.io/wreath/guides/deployment.html). The
[camera-trap service](example/README.md) is the canonical runnable application.
Wreath is pre-1.0; the [version, platform and upgrade contract](https://alexogeny.github.io/wreath/start/releases.html)
states exactly what the current docs cover.

## Performance claims come with the workload

Wreath moves repeated work to startup and byte-heavy work into owned native kernels.
The comparisons below use retired userspace instructions, equivalent verified output,
alternating samples and retained raw evidence. They do not turn one elapsed-time run
into a throughput claim.

## Native gzip, shaped to the document

Wreath owns both halves of gzip. The encoder and decoder are independent native
kernels: they share the RFC 1951/1952 wire format, not one duplex algorithm.
When `Content-Type` is known, JSON, GraphQL, HTML, logs, plain text, and
high-entropy JSON take separately compiled policies. The result is still
ordinary gzip readable by browsers, zlib, zlib-ng, and libdeflate.

Retired userspace instructions per uncompressed byte at level 6, lower is
better. Each cell is **Wreath / libdeflate / zlib-ng**:

| Document | Encode instr/B | Decode instr/B | Wreath encode L1D hit | Wreath encode LLC hit |
|---|---:|---:|---:|---:|
| JSON | **52.94** / 64.58 / 67.79 | **6.13** / 7.40 / 9.17 | 91.50% | 85.62% |
| GraphQL | **32.31** / 49.76 / 46.45 | **3.04** / 3.56 / 4.37 | 89.18% | 90.38% |
| HTML | **45.37** / 59.27 / 61.27 | **4.54** / 5.57 / 6.85 | 89.70% | 87.75% |
| plain text | **71.55** / 92.56 / 101.70 | **6.84** / 7.82 / 9.63 | 88.56% | 93.45% |
| logs | **50.37** / 62.97 / 67.06 | **6.12** / 7.55 / 9.33 | 92.22% | 82.79% |
| high-entropy JSON | **57.00** / 89.78 / 124.37 | **10.46** / 14.23 / 18.03 | 97.80% | 99.03% |
| **weighted corpus** | **50.88** / 67.18 / 71.96 | **5.62** / 6.82 / 8.44 | — | — |

That is fewer encode and decode instructions in every document row. The cache
figures are the 500 kB representatives: the high-entropy encoder retires only
4.13 LLC misses/kB, while the regular-document decoder stays at 1.79–2.22 LLC
misses/kB with a 97.86–98.17% LLC hit rate. Across the corpus, Wreath's output
is 0.33% larger than zlib-ng's and 0.80% larger than libdeflate's—the ratio cost
kept beside the instruction win rather than hidden.

The comparison uses repository-native boundaries, application- or stream-owned
warm contexts, six document sizes from 10 kB through 1 MB (the high-entropy
case is 500 kB), and hardware performance counters on Linux x86-64 / Ryzen 7
7730U. A subsequent cache sweep over 30 JSON, GraphQL, HTML, text, and log
documents chose the 32 Ki-entry format hash table and shallower per-format
search limits: encode instructions fell **4.79%**, L1D misses **12.87%**, and
LLC misses **52.07%** on the same corpus. The shipped balance retires 49.04
encode instructions/byte with 91.62% L1D and 93.92% LLC hit rates; output is
23.71% of input. No elapsed time, cycles, or IPC enters either result. The
repository retains its measurement data with the benchmark sources.

## One real request, all the way through

This is not a plaintext race or a synthetic JSON payload. All three
configurations render the same operations-intelligence dashboard from one
successful request:
HTTP policy and sessions, nested typed input, bearer authentication, Cedar,
overlapping PostgreSQL and HTTP wire calls, eleven requested rows projected
from a sparse 730-day × 48-tenant × 6-measure source, eleven chart paths,
temporal, geospatial and vector work, ranked pagination, protobuf and
MessagePack exports, an escaped
template, compression, and HTML emission.

Retired userspace instructions per request, lower is better. Five samples are
too few for a meaningful histogram, and scaling every row independently made
the old glyphs impossible to compare. This instead prints the observed range,
its width relative to the median, and the median directly:

| Stack | Five-sample range | Range width / median | Median |
|---|---:|---:|---:|
| Wreath: format-aware gzip | 7.211M–7.275M | 0.89% | **7.261M** |
| Wreath: DCZ + fragment gzip | 4.022M–4.109M | 2.14% | **4.039M** |
| FastAPI: Uvicorn | 209.549M–211.069M | 0.72% | **210.328M** |
| Sanic: native server | 209.758M–216.695M | 3.28% | **211.290M** |
| BlackSheep: Granian | 207.793M–211.982M | 2.00% | **209.515M** |

The cache result is clearest in absolute terms: optimized Wreath records
**159,258 L2 misses per request**. The other stacks record 2.24–2.81 million—
**14.06× to 17.65× more misses per request**.

| Stack | Instructions / request | L2 misses / request | L2 misses / 1k instructions |
|---|---:|---:|---:|
| Wreath: DCZ + fragment gzip | **4,038,800** | **159,258** | 39.43 |
| Wreath: format-aware gzip | **7,261,469** | **167,176** | 23.02 |
| FastAPI: Uvicorn | 210,328,229 | 2,238,836 | 10.64 |
| Sanic: native server | 211,289,930 | 2,811,521 | 13.31 |
| BlackSheep: Granian | 209,514,762 | 2,592,360 | 12.37 |

The normalized Wreath ratio looks high because Wreath retires roughly 52× fewer
instructions while some cache traffic remains fixed. Dividing that traffic by
the much smaller instruction count raises misses per thousand instructions; it
does **not** mean more cache misses per request. The retained samples include the
full L1 and [AMD L2](https://docs.amd.com/r/en-US/57368-uProf-user-guide/4.6.3.-Performance-Metrics-for-AMD-EPYC-Zen-4-and-later-Core-Architecture-Processors)
demand and prefetch counters.

Format-aware Wreath retired **28.96× fewer instructions** than FastAPI in this
run. The dictionary-aware configuration retired **52.08× fewer instructions**,
removing **3,222,669 instructions per request**—a further **44.38%** from Wreath's
full request path. BlackSheep on Granian was the least instruction-heavy of the
three ecosystem stacks, at 28.85× and 51.88× the two Wreath medians. The DCZ
response is RFC 9842 `dcz`: a real response for
neighbouring resource 41 is the client-held dictionary for resource 42, whose
35,805-byte body differs at only two bytes. The same configuration falls back
to standard concatenated gzip members for ordinary clients, recompressing the
249-byte dynamic prefix while reusing the independently readable stable member.
It does not claim that DCZ and gzip are nested in one response.

All five rows use TLS 1.3 over HTTP/1.1 and are medians of five alternating
30/15-request slopes. The unchanged A/A controls differed from the primary
medians by 0.58%, 0.90%, 0.14%, 0.44%, and 0.11%, respectively. The harness
accepts a DCZ sample only after checking its dictionary hash, decoded response
facts, secure transport, and both required `Vary` fields; it separately
exercises and decodes the fragment-gzip fallback before attaching counters.

The ecosystem implementations are not slow pure-Python foils. FastAPI uses
Starlette policy middleware, Pydantic, and Uvicorn/uvloop/httptools. Sanic uses
its native production server in single-process mode. BlackSheep uses Granian's
single-threaded ASGI runtime with uvloop, the faster small-process mode its
server documents. Sanic and BlackSheep share one msgspec-typed business kernel;
all three use `cedarpy`, `asyncpg`, `aiohttp`, NumPy, Jinja, protobuf, msgspec,
and standard gzip. Wreath performs the corresponding transaction through its
dependency-free declarative surfaces and native data kernels.

To expose the successful-path application cost layer by layer, a smaller
companion request removes the dashboard calculations while keeping the same
routing, CORS, validation, auth, Cedar, PostgreSQL, HTTP, and JSON result. The
starred cells below are deliberately called out because they are not
like-for-like framework feature costs:

The cumulative decomposition shows where work enters:

| Successful request includes | Wreath | BlackSheep + Granian | Sanic native | FastAPI + Uvicorn |
|---|---:|---:|---:|---:|
| route + JSON response | 35,221 | 104,326 | 220,806 | 425,757 |
| + CORS | 46,542 | 143,283 | 241,150 | 491,308 |
| + typed binding and validation | 107,943 | 222,511<sup>*</sup> | 275,629<sup>*</sup> | 774,667 |
| + bearer authentication | 114,036 | 229,477<sup>*</sup> | 277,595<sup>*</sup> | 867,014 |
| + Cedar authorization | 153,793 | 931,174 | 974,843 | 1,597,999 |
| + PostgreSQL query | 206,037 | 1,104,737 | 1,145,880 | 1,811,778 |
| + outbound HTTP | **249,844** | **1,444,119** | **1,443,892** | **2,107,179** |

<sup>*</sup> Sanic and BlackSheep share a hand-written, success-path adapter:
msgspec decodes the known-good body, the query is converted inline, and bearer
"authentication" is an exact comparison with `"Bearer user"`. Wreath's cells
exercise its public binder and bearer backend, including structured validation
refusals, case-insensitive scheme parsing, duplicate-credential refusal,
`Identity` publication, protected-route resolution, and a proper 401 Bearer
challenge. Invalid input is therefore not equivalent: for example, the Sanic
adapter answers 500 for a bad query or missing token where Wreath answers 422 or
401, and it accepts a duplicated Authorization field when the first value is
valid. Every later cumulative BlackSheep and Sanic cell inherits this shortcut.
Those full rows remain an account of the exact successful applications, but the
starred increments are not evidence that equivalent framework validation or
authentication is cheaper. In the retained Sanic run, the 1,966-instruction
authentication increment is also below its available 2,747-instruction A/A
resolution and is unresolved.

On this stripped companion, Wreath retired **8.43× fewer instructions** than
FastAPI and **5.78× fewer** than both Sanic and BlackSheep. BlackSheep has the
lowest ecosystem route floor; the two lightweight stacks converge once the
same Cedar, PostgreSQL and outbound HTTP work dominates. Its purpose is
decomposition; the holistic request above is the headline system comparison.

| Arm | Installed stack |
|---|---|
| Wreath | Wreath 0.3.4: metal server, binding, auth, startup-compiled Cedar, PostgreSQL, and HTTP client; no mandatory third-party runtime dependencies |
| FastAPI | FastAPI 0.139, Starlette, Pydantic/pydantic-core, Uvicorn, uvloop, httptools, `HTTPBearer`, `cedarpy`, `asyncpg`, `aiohttp`, NumPy, Jinja, protobuf, and msgspec |
| Sanic | Sanic 25.12.1 native server, a hand-written msgspec success-path binding/auth adapter, `cedarpy`, `asyncpg`, `aiohttp`, NumPy, Jinja, and protobuf |
| BlackSheep | BlackSheep 2.6.3 on Granian 2.7.9 ASGI/uvloop, plus the same success-path adapter and typed business stack as Sanic |

CORS is Starlette's `CORSMiddleware`, which FastAPI already brings in. It is not
padded into the stack as another package. `cedarpy` 4.8.7 has a stateless public
authorization call and no reusable compiled-policy handle. The measured request
therefore includes that public lifecycle.

Both clients speak their real wire protocols to the same deterministic,
in-process PostgreSQL and HTTP peers. The verifier rejects a sample unless the
security, CORS, session, compression, and business response facts agree.
Imports, startup, compilation, pool creation, and warm-up cancel through N/N/2
slopes. Server and generator are pinned separately with `PYTHONHASHSEED=0`.
Instructions and the four L1 events share one non-multiplexed perf pass; five
AMD component events split demand from prefetch L2 traffic in a second pass.
L1 hits are accesses minus misses; aggregate L2 misses are retained only as an
absolute and per-instruction normalization, not presented as one hit rate.
The result contains no elapsed time, cycles, or IPC. It was recorded on CPython
3.14.7 and Linux x86-64, on a Ryzen 7 7730U. SIMD dispatch can make another
architecture retire a different count.

Reproduce it:

```bash
uv sync --inexact --group benchmark
uv run python -m benchmarks.bench_holistic_stack_instructions \
  --requests 30 --trials 5 --connections 8 --warmup 16 \
  --output benchmarks/baselines/e2e-holistic-stack-instructions.json

# The cumulative framework-layer control
uv run python -m benchmarks.bench_e2e_instructions \
  --requests 4000 --trials 5 --connections 32 --warmup 500 \
  --output benchmarks/baselines/e2e-stack-instructions.json
```

The repository retains the holistic [raw samples](benchmarks/baselines/e2e-holistic-stack-instructions.json),
[Wreath application](benchmarks/holistic_e2e.py),
[FastAPI application](benchmarks/holistic_fastapi.py), and
[counter harness](benchmarks/bench_holistic_stack_instructions.py), plus the
[cumulative control samples](benchmarks/baselines/e2e-stack-instructions.json).

## Install

```bash
pip install wreath
# or
uv add wreath

uv add 'wreath[linux]'     # io_uring reactor and native TLS on Linux
uv add 'wreath[h3]'        # HTTP/3; `http3` is an alias
```

The base wheel includes Wreath's portable C implementation. The framework
remains a conforming ASGI application when you use another server.

## The engineering rule

Wreath moves repeated work out of requests and moves byte-heavy work into native
kernels. It does not call an idea faster because it was rewritten in C. Every
performance change starts with a measurement, keeps its controls, and reports
the machine and method that produced the result.

The same refusal applies to correctness. Unsupported declarations fail at
startup. Hot-path complexity has executable probes. The test runner samples
declared controls and asks whether the suite notices when one disappears.

## Work on Wreath

```bash
uv sync
uv run wreath-check
uv run wreath test
```

The repository's [`AGENTS.md`](AGENTS.md) is the engineering contract.
