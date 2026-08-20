<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wreath-dark.png">
  <img src="docs/assets/wreath-light.png" alt="Wreath" width="320">
</picture>

# Wreath

**Many separate parts, gathered and woven until they hold a single shape.**

[![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-2f855a?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![ASGI](https://img.shields.io/badge/ASGI-any_server-7c3aed?style=flat-square)](https://asgi.readthedocs.io/)
![HTTP 1.1, 2, 3](https://img.shields.io/badge/HTTP-1.1%20%7C%202%20%7C%203-0891b2?style=flat-square)
![Runtime dependencies: zero](https://img.shields.io/badge/runtime_dependencies-zero-16a34a?style=flat-square)
[![License: MPL-2.0](https://img.shields.io/badge/license-MPL--2.0-64748b?style=flat-square)](https://github.com/alexogeny/wreath/blob/main/LICENSE)

A Python 3.14-first ASGI framework, PostgreSQL stack, job system, policy engine,
and native HTTP server. One package; no mandatory runtime dependencies.

**[Documentation](https://alexogeny.github.io/wreath/)** ·
[Browse by task](https://alexogeny.github.io/wreath/map.html) ·
[Install](https://alexogeny.github.io/wreath/getting-started/index.html) ·
[Issues](https://github.com/alexogeny/wreath/issues)

</div>

---

## One system. One obvious home.

A production web service needs more than a router. It needs validation and data
access. It also needs identity, policy, background work, observability, and a server.
Wreath ships those parts together and names each module after the job it does:
`wreath.pagination` paginates, `wreath.jobs` runs jobs, and `wreath.email` sends
mail.

The imagery belongs to the name. The API stays literal.

```python
from wreath import Request, Wreath

app = Wreath()

@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
```

```bash
wreath run app:app          # native HTTP server
wreath dev app:app          # native server with reload
uvicorn app:app             # or any conforming ASGI server
```

Wreath is pre-1.0. Shipped surfaces are implemented and tested. Work that is
named but not finished lives only in the
[roadmap](https://alexogeny.github.io/wreath/reference/roadmap.html).

## Choose your route

| You want to… | Start here |
|---|---|
| build your first service | [Installation and first app](https://alexogeny.github.io/wreath/getting-started/index.html) |
| find one feature or concept | [Documentation map](https://alexogeny.github.io/wreath/map.html) |
| complete a concrete task | [Cookbook](https://alexogeny.github.io/wreath/cookbook/index.html) |
| look up a class or function | [API reference](https://alexogeny.github.io/wreath/reference/app.html) |
| move from FastAPI, Pydantic, SQLModel, or Alembic | [Migration path](https://alexogeny.github.io/wreath/from-fastapi/index.html) |
| evaluate the dependency or performance claim | [Capability map](https://alexogeny.github.io/wreath/capabilities.html) and [measurements](https://alexogeny.github.io/wreath/perf/index.html) |

The published site keeps **Browse** in its header on every page. Search is
`Ctrl K` or `/`.

## What ships

| Part of the service | Wreath owns |
|---|---|
| request path | ASGI application, routing, binding, validation, middleware, responses, OpenAPI, typed clients |
| identity and policy | sessions, JWT and API keys, OAuth/OIDC, users, TOTP, passkeys, Cedar authorization |
| data | native PostgreSQL driver, ORM, migrations, safe SQL, vector and full-text search, pagination |
| long-running work | durable jobs, schedules, messaging, workflows, resumable streams, progress |
| service boundaries | outbound HTTP, webhooks, MCP, object storage, signatures, provenance, email |
| operations | native HTTP/1.1, HTTP/2 and HTTP/3 server, health, telemetry, logging, replay, testing |

The full generated inventory says what each part replaces and links to its
guide: [what you do not have to install](https://alexogeny.github.io/wreath/capabilities.html).

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
repository retains the [measurement data](docs/perf/data/gzip-native-instructions.json).

## One real request, all the way through

This is not a plaintext race or a synthetic JSON payload. All three
configurations render the same operations-intelligence dashboard from one
successful request:
HTTP policy and sessions, nested typed input, bearer authentication, Cedar,
overlapping PostgreSQL and HTTP wire calls, a sparse-to-dense 730-day ×
48-tenant × 6-measure projection, eleven chart paths, temporal, geospatial and
vector work, ranked pagination, protobuf and MessagePack exports, an escaped
template, compression, and HTML emission.

Retired userspace instructions per request, lower is better:

```text
Wreath: DCZ + fragment gzip  ▌                                               2,426,742
Wreath: format-aware gzip    █                                               4,690,494
FastAPI: gzip                ████████████████████████████████████████████████  226,270,075
```

Format-aware Wreath retired **48.24× fewer instructions** than FastAPI in this
run. The dictionary-aware configuration retired **93.24× fewer instructions**,
removing **2,263,752 instructions per request**—a further **48.26%** from Wreath's full
request path. Its selected response is RFC 9842 `dcz`: a real response for
neighbouring resource 41 is the client-held dictionary for resource 42, whose
35,805-byte body differs at only two bytes. The same configuration falls back
to standard concatenated gzip members for ordinary clients, recompressing the
249-byte dynamic prefix while reusing the independently readable stable member.
It does not claim that DCZ and gzip are nested in one response.

All three rows use TLS 1.3 over HTTP/1.1 and are medians of five alternating
30/15-request slopes. Ranges were 4,673,133–4,696,949 for format-aware Wreath,
2,416,962–2,447,953 for the dictionary-aware configuration, and
226,205,485–226,918,987 for FastAPI. The unchanged A/A controls differed from
their medians by 0.03%, 0.10%, and 0.11%, respectively. The harness accepts a
DCZ sample only after checking its dictionary hash, decoded response facts,
secure transport, and both required `Vary` fields; it separately exercises and
decodes the fragment-gzip fallback before attaching counters.

The FastAPI implementation is not a slow pure-Python foil. It uses the
ecosystem stack a pragmatic service would assemble: Starlette policy
middleware, Pydantic, Uvicorn/uvloop/httptools, `HTTPBearer`, `cedarpy`,
`asyncpg`, `aiohttp`, NumPy, Jinja, protobuf, and msgspec. Wreath performs the
corresponding transaction through its dependency-free declarative surfaces and
native data kernels.

To make the framework layers independently auditable, a smaller companion
request removes the dashboard calculations while keeping the same successful
routing, CORS, validation, auth, Cedar, PostgreSQL, HTTP, and JSON path:

The cumulative decomposition shows where work enters:

| Successful request includes | Wreath | FastAPI stack |
|---|---:|---:|
| route + JSON response | 36,006 | 426,899 |
| + CORS | 47,156 | 490,826 |
| + typed binding and validation | 115,305 | 776,764 |
| + bearer authentication | 122,448 | 867,935 |
| + Cedar authorization | 173,902 | 1,597,272 |
| + PostgreSQL query | 225,857 | 1,809,675 |
| + outbound HTTP | **269,110** | **2,133,383** |

On this stripped companion, Wreath retired **7.93× fewer instructions**. Its
purpose is decomposition; the holistic request above is the headline system
comparison.

| Arm | Installed stack |
|---|---|
| Wreath | Wreath 0.3.2 for the holistic arm (0.3.1 for the retained cumulative control): metal server, binding, auth, startup-compiled Cedar, PostgreSQL, and HTTP client; no mandatory third-party runtime dependencies |
| FastAPI | FastAPI 0.139, Starlette, Pydantic/pydantic-core, Uvicorn, uvloop, httptools, `HTTPBearer`, `cedarpy`, `asyncpg`, `aiohttp`, NumPy, Jinja, protobuf, and msgspec |

CORS is Starlette's `CORSMiddleware`, which FastAPI already brings in. It is not
padded into the stack as another package. `cedarpy` 4.8.7 has a stateless public
authorization call and no reusable compiled-policy handle. The measured request
therefore includes that public lifecycle.

Both clients speak their real wire protocols to the same deterministic,
in-process PostgreSQL and HTTP peers. The verifier rejects a sample unless the
security, CORS, session, compression, and business response facts agree.
Imports, startup, compilation, pool creation, and warm-up cancel through N/N/2
slopes. Server and generator are pinned separately with `PYTHONHASHSEED=0`.
The result contains no elapsed time, cycles, or IPC. It was recorded on CPython
3.14.7 and Linux x86-64, on a Ryzen 7 7730U. SIMD dispatch can make another
architecture retire a different count.

Reproduce it:

```bash
uv sync --inexact --group benchmark
uv run python -m benchmarks.bench_holistic_stack_instructions \
  --requests 30 --trials 5 --connections 8 --warmup 16 \
  --output docs/perf/data/e2e-holistic-stack-instructions.json

# The cumulative framework-layer control
uv run python -m benchmarks.bench_e2e_instructions \
  --requests 4000 --trials 5 --connections 32 --warmup 500 \
  --output docs/perf/data/e2e-stack-instructions.json
```

The repository retains the holistic [raw samples](docs/perf/data/e2e-holistic-stack-instructions.json),
[Wreath application](benchmarks/holistic_e2e.py),
[FastAPI application](benchmarks/holistic_fastapi.py), and
[counter harness](benchmarks/bench_holistic_stack_instructions.py), plus the
[cumulative control samples](docs/perf/data/e2e-stack-instructions.json).

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

Read the [request path](https://alexogeny.github.io/wreath/internals/index.html)
for the design and [performance](https://alexogeny.github.io/wreath/perf/index.html)
for the numbers.

## Work on Wreath

```bash
uv sync
uv run wreath-check
uv run wreath-check --docs
uv run wreath test
```

The repository's [`AGENTS.md`](AGENTS.md) is the engineering contract. The
[agent cookbook](https://alexogeny.github.io/wreath/cookbook/agents/index.html)
turns it into task-shaped routes through the tree.
