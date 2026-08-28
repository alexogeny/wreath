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

The median hardware-counter account is:

| Stack | Instructions | L1D hit / miss | L1I hit / miss | L2 demand hit / miss | L2 prefetch hit / miss | All L2 misses / kI |
|---|---:|---:|---:|---:|---:|---:|
| Wreath: format-aware gzip | **7,261,469** | 2,812,957 / 155,213 (94.77%) | 420,470 / 4,738 (98.89%) | 218,318 / 112,276 (66.04%) | 101,025 / 63,234 (61.50%) | 167,176 / 23.02 |
| Wreath: DCZ + fragment gzip | **4,038,800** | 1,600,387 / 85,896 (94.91%) | 402,420 / 4,440 (98.91%) | 153,955 / 115,825 (57.07%) | 26,115 / 43,433 (37.55%) | 159,258 / 39.43 |
| FastAPI: Uvicorn | **210,328,229** | 79,132,583 / 3,724,669 (95.50%) | 32,287,156 / 692,386 (97.90%) | 17,818,754 / 1,845,223 (90.62%) | 1,192,706 / 362,210 (76.71%) | 2,238,836 / 10.64 |
| Sanic: native server | **211,289,930** | 79,422,207 / 3,707,624 (95.54%) | 32,124,452 / 691,043 (97.89%) | 17,586,395 / 2,126,588 (89.21%) | 1,129,875 / 570,852 (66.43%) | 2,811,521 / 13.31 |
| BlackSheep: Granian | **209,514,762** | 78,703,071 / 3,649,008 (95.57%) | 31,556,106 / 673,125 (97.91%) | 17,474,065 / 2,175,424 (88.93%) | 1,223,612 / 472,673 (72.13%) | 2,592,360 / 12.37 |

The denominators matter. An L1D percentage divides L1 data accesses; L1I
divides instruction fetches. The two L2 percentages divide, separately, core
demand requests and hardware-prefetch requests observed by
[AMD's L2 events](https://docs.amd.com/r/en-US/57368-uProf-user-guide/4.6.3.-Performance-Metrics-for-AMD-EPYC-Zen-4-and-later-Core-Architecture-Processors).
Neither is “the fraction of the L1 misses above that hit L2”, and combining
demand with prefetch into one rate hides which population changed. “All L2
misses / kI” therefore reports both the absolute median and misses per thousand
retired instructions. The latter can rise when useful work is deleted: DCZ
retires 44% fewer instructions than ordinary Wreath while its roughly fixed
cache traffic is divided by a smaller instruction count.

This is why Wreath's lower L2 percentage is not more L2 pressure. The optimized
Wreath row records **159,258 total L2 misses per request**, versus
BlackSheep's **2,592,360**—16.28× fewer—even though BlackSheep's demand hit
percentage is higher. Moving work toward L1 means shrinking active data/code,
eliminating copies, and keeping transient buffers contiguous; it does not mean
optimizing the percentage itself. Every stack materializes only the eleven
chart rows that reach the response. Wreath additionally reconciles consecutive
measures from one series in one bucket walk, borrows exact numeric cells while
their request owner stays alive, hashes each dense bucket and measure once into
a call-owned lookup plan, and keeps row indices, values, presence bits and the
LTTB selection workspace call-owned and contiguous. Coarse temporal counts skip
zone reconstruction when the calendar gap alone proves the final bucket is
before the end. Sparse-vector indices and values share one compact native
allocation, while metrics insert first-seen exact integers directly and pay
arbitrary-precision conversion only for duplicate sums. The ranker bounds and
transforms its existing embedding in native workspace instead of building a
Python score array; template emission borrows safe top-level scalars; and
content-format dispatch no longer allocates normalized Python strings. Prepared
template fragments can also carry policy-owned provenance, so gzip compresses
only their dynamic edges and does not reread the full uncompressed stable
suffix; ordinary byte bodies retain the exact comparison and safe
full-compression fallback.

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
| Wreath | Wreath 0.3.3: metal server, binding, auth, startup-compiled Cedar, PostgreSQL, and HTTP client; no mandatory third-party runtime dependencies |
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
