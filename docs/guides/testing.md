# Testing

A framework you can't test comfortably is a framework you'll come to distrust.
Wreath's test client runs your application entirely in process — no sockets, no
ports, no fixtures to tear down — while still going through the *whole* pipeline:
middleware, authentication, binding, and your handler, exactly as production
would.

## User story: prove the auth gate actually rejects

> *As an API author, I need a test that a protected endpoint returns `401` with no
> token and `200` with one — going through the real middleware and auth, not a
> mock — so the test fails the day I misconfigure the guard.*

```python
from wreath.testing import TestClient

async def test_requires_auth():
    async with TestClient(app) as client:
        assert (await client.get("/account")).status == 401

        ok = await client.get("/account", headers={"Authorization": f"Bearer {token}"})
        assert ok.status == 200
        assert ok.json()["id"] == "u_123"
```

Nothing is stubbed: the request travels the same middleware, authentication, and
binding path production uses, so a passing test means a real caller sees the same
result. `headers=` sets request headers, `json=` sends a body, and `.json()` /
`.status` read the response.

```python
from wreath.testing import TestClient

async def test_create():
    async with TestClient(app) as client:
        response = await client.post("/items", json={"name": "widget"})
        assert response.status == 201
```

Because the client exercises the real request path, a passing test means the
behaviour a user would see is correct — not merely that a function returns the
right value in isolation. The lifespan runs too, so startup and shutdown logic is
covered. WebSocket handlers get their own `WebSocketTestSession` for driving a
conversation and asserting on what comes back.

## User story: the same request, as three different people

> *Authorization tests are the same call repeated per role. Doing that with
> tokens means every test carries a `Bearer` literal that has nothing to do with
> what it is checking, and minting a real token per role is a fixture nobody
> wants to maintain.*

`acting_as` gives you a client that *is* someone:

```python
async def test_only_editors_may_edit_a_llama():
    async with TestClient(app) as client:
        admin = client.acting_as("root", roles=["admin"])
        editor = client.acting_as("ada", roles=["editor"])
        rider = client.acting_as("bo", roles=["rider"])

        assert (await rider.patch("/llamas/7", json={"name": "Bea"})).status == 403
        assert (await editor.patch("/llamas/7", json={"name": "Bea"})).status == 200
        assert (await admin.delete("/llamas/7")).status == 200
```

Each derived client shares the application and its lifespan, so make as many as
you have roles. The identity travels on the request rather than on the backend,
so `admin` and `rider` can have calls in flight simultaneously without
interfering — which matters as soon as you write a concurrency test.

Pass a whole `Identity` when you need permissions or a non-default principal
type; pass an id with `roles=`/`permissions=` for the common case. Passing both
is an error, because two sources for the same fact is how a test ends up lying
about what it covers.

!!! warning "It bypasses authentication"

    While an acting-as client exists, the application's authentication backend
    is replaced with one that trusts the request scope, and it is restored when
    the client exits. That is the right trade for testing *authorization* and
    the wrong one for testing *authentication* — use a real token there, as in
    the first example.

For headers you want on every request without touching identity, there is
`client.with_headers(x_tenant="acme")`.

## The fixtures come with the install

Wreath registers a pytest plugin through the `pytest11` entry point — the same
mechanism `pytest-django` and `pytest-asyncio` use — so its fixtures resolve in a
project with no `conftest.py` at all. Define one fixture, `wreath_app`, and the
rest follow:

```python
# conftest.py
import pytest
from myproject.app import app as application

@pytest.fixture
def wreath_app():
    return application
```

```python
async def test_products(wreath_client):
    response = await wreath_client.get("/products")
    assert response.status == 200
```

| Fixture | What it gives you |
| --- | --- |
| `wreath_app` | **Yours to override.** The shipped default raises with the lines to write |
| `wreath_client` | A `TestClient` entered around the test, so startup and shutdown handlers run |
| `wreath_email` | A `CapturingEmailSender`; read `verifications` / `resets` |
| `wreath_postgres_dsn` | `WREATH_TEST_POSTGRES_DSN`, or a skip whose reason names it |
| `wreath_database` | A started `Database` on that DSN, stopped afterwards |
| `wreath_db` | A connection in a transaction that is **rolled back** after the test |

Override any of them at any scope — a `session`-scoped `wreath_app` is the usual
choice once building the app costs anything.

Two things worth knowing before you rely on them. Every name is `wreath_`-prefixed
on purpose: the plugin loads in *every* project that installs Wreath, and a bare
`client` or `db` would shadow a fixture of your own in a file you did not write.
And `wreath_db` rolls back in a `finally`, so a test that raises rolls back too —
which is the difference from cleaning up on the last line of each test, where the
first failure leaves rows behind and every later test in the file fails for
reasons unrelated to what it asserts. Code under test that commits its own
transaction defeats this; that case wants a fixture that truncates instead, and
nothing here can detect it for you.

The async fixtures need an async pytest plugin, which is not Wreath's to install.
`pytest-asyncio` is used when present — including its own decorator, so they work
under `asyncio_mode = strict` as well as `auto`.

## What a suite of these actually costs

Entering `TestClient` runs the real lifespan, and the lifespan is where a test
suite's time goes. Three things dominate, in this order.

**Building the application.** Route compilation, signature inspection and the
middleware tape are cached on the `Wreath` object, so they are paid once per
object rather than once per lifespan. A `wreath_app` built fresh in a
function-scoped fixture re-pays all of it for every test; a `session`-scoped one
does not. This is the single biggest lever and it is one word.

```python
@pytest.fixture(scope="session")
def wreath_app():
    return application
```

**The boot audit.** `hardening="warn"` is the default, and it AST-scans your
package on every startup. The findings are cached per file on `(mtime, size)`,
so the scan is paid once per process and the second and later startups are
free — a scan of Wreath's own 379 modules falls from seconds to about 20ms on
the repeat. You do not need to turn it off for speed. If you want it off anyway
— a test suite is not usually where you want boot warnings — `WREATH_HARDENING`
outranks the `hardening=` argument:

```python
@pytest.fixture(autouse=True)
def _quiet_boot_audit(monkeypatch):
    monkeypatch.setenv("WREATH_HARDENING", "off")
```

Wreath's own suite does exactly that, and exempts the tests that assert what the
policy *does* — set globally it would neuter those, and they would keep passing.

**Schema validation.** Each ORM registry reads `pg_catalog` at startup to check
your models against the live database, and `validate_schema="error"` is the
default. It is worth paying once; it is not worth paying per test. Set
`validate_schema="off"` on the registries a test suite builds repeatedly, and
keep one test that starts the application with validation on — that is the one
that would catch a model drifting from its table.

Under `-n`, give every worker its own schema and derive the name from
`PYTEST_XDIST_WORKER`. **Assign it, never `setdefault` it in a `conftest.py`**:
the controller imports the conftest during collection and then spawns workers
with its own environment, so a `setdefault` writes the controller's value, every
worker inherits it, and all of them share one schema. That failure looks like
the fix not working rather than like a mistake in the fix, and what PostgreSQL
reports is a `pg_namespace_nspname_index` unique violation, which reads like
anything except a test-isolation bug.

## Read the suite report

Once a suite is large enough, a row of dots hides the two things you usually
want to know: *what is still running?* and *where did the time go?* `wreath test`
runs the native collector and C dispatch workers by default and puts every
collected test file on a stable state-map tile:

```bash
wreath test
wreath test tests/auth/ -k refresh
wreath test --workers 1 tests/test_login.py
```

Pytest remains available as the explicit compatibility oracle:

```bash
wreath test --engine pytest --mutant off tests/test_plain.py
wreath test --engine dual --mutant off tests/native-contract/
```

Native mode handles module functions, test classes, sync/async bodies,
fixture dependency graphs and scopes, conftest fixtures, parametrization,
capture/temporary-path/monkeypatch built-ins, skip marks, `raises`, `warns`,
`fail`, `skip`, and `approx`. Whole modules run in isolated native workers;
unsupported semantic hooks and pytest arguments refuse before any body runs.
Dual mode runs each test twice and is therefore
only for a hermetic compatibility corpus. See [the native engine
contract](../internals/native-test-engine.md) for the exact surface and the
evidence required before it can become the default.

The runner performs no live terminal rendering. It prints one uncoloured final
state map after testing, mutation, and fuzzing finish. Ordinary states use solid
squares, failures and mutation misses use multiplication signs, and a file that
passes all enabled stages uses a star. `--grid never` remains accepted as an
explicit compatibility spelling and is also the default; the removed `auto`
and `always` animation modes are refused rather than silently ignored.

The final report includes wall time, summed test time, average, median, p95 and
p99 duration, practical tail counts at 100 ms, 250 ms, and one second, a Tukey
outlier count with its threshold, worker utilization, and the slowest tests.
The threshold matters: a large fast suite can have many statistical outliers
above a fence of only a few milliseconds; those are not all "slow tests". Read
the practical tail, p95, p99, threshold, and named slowest tests together.
Setup, call, and teardown time all count: a slow fixture is part of what the test
costs, not invisible overhead. Wreath keeps a bounded per-file history in
`.wreath/test-history.json`; use `--no-history` for an entirely stateless run or
`--history PATH` to put it elsewhere. The cache is local and ignored by version
control. On a broad run where at least 80% of conventional test modules have
current timing history, `auto` collection assigns each whole module to one
fresh xdist worker before import. That worker alone constructs the module's
parameters and immutable test data, and module-scoped fixtures remain alive for
all of its tests. Modules are longest-processing-time balanced from the newest
broad run, so preserving locality does not leave one worker with the slow tail.
A focused path, a cold or disabled history, fewer than four modules per worker,
a caller-supplied xdist mode, or an `xdist_group` spanning modules keeps
replicated xdist collection and the existing dynamic historical queue instead.

Use `--collection replicated` for an exact A/B against xdist's collection, or
`--collection sharded` to force conventional Python modules into disjoint
workers. The forced form refuses a cross-module `xdist_group`, naming
`replicated` as the correct form, because silently separating that group would
change its contract. Sharding does not share a mutable fixture or fork a live
pytest process: every test still runs in a fresh worker interpreter. It deletes
duplicate construction rather than making test state shared.
Because a replacement xdist process receives a new worker id, sharded mode sets
`--max-worker-restart=0`: a crashed shard fails the run instead of returning
green after collecting somebody else's modules. An explicit restart policy
therefore selects replicated mode, and forcing both is refused.

On an 8,000-test synthetic corpus (80 modules, 100 parameters and one immutable
5,000-row object per module), three interleaved warm rounds measured
`replicated` at 11.94s ± 1.11s and `sharded` at 6.62s ± 0.19s wall time on the
same eight workers, a 45% reduction. This prices repeated collection; it is not
a claim that every Wreath test file has that object shape.

For a complete artifact, `--report PATH` writes versioned JSON with every test
and file outcome and duration:

```bash
wreath test --report test-run.json -m "not network"
```

### Add mutation confidence to the same run

Any run with passing tests can continue into Wreath's control-aware mutation
tester:

```bash
wreath test                                         # test + 192 controls + gated fuzz
wreath test --mutant sample                         # the same 192 stable controls
wreath test --mutant on                             # explicit spelling of auto
wreath test --mutant-samples 48                     # a smaller sample; barely faster
wreath test --mutant sample --mutant-samples 384   # a larger confidence sample
wreath test --mutant changed --mutant-changed main # controls changed on this branch
wreath test --mutant full                           # the complete sweep
wreath test --fuzz off                              # stop after mutation confidence
wreath test --mutant on --fuzz on                  # explicit spelling of the default pipeline
wreath fuzz                                        # shorthand for that full pipeline
```

The sample is ranked by a stable hash across the whole eligible corpus. It is
not `wreath mutant --limit`, which takes the first controls in source order and
therefore spends a small budget at file heads. The final line gives an
actionable rating -- `SAMPLE WATCHED`, `REVIEW ASSERTIONS`, `ADD
COVERAGE`, or `FINISH THE SAMPLE` -- while keeping `killed`, `survived`,
`unreached`, and undecided controls separate. It does not average distinct
findings into a percentage. During this phase candidate test files turn pink;
a file moves to purple `Mutant pass` after one of its tests kills the mutant,
while files without a kill finish as pink crosses in `Mutation miss`. The
summary names exactly how many files lack mutation evidence, and a terminal
mutation-enabled grid contains no green block. A JSON test report gains the
complete `mutation` document, its structured rating, and
`verified_test_files` and `failed_mutation_test_files` for that same evidence.
The latter records candidate exposure to survivor evidence, but does not erase
an exact kill that the file supplied for another control. The visible cross
means the file did not earn positive mutation evidence, not that an individual
assertion failed.

Mutation uses only tests that passed in the ordinary run as eligible killers.
That keeps the evidence honest without throwing away the rest of a large run:
red files stay red, while green candidate files can move through the mutation
rows without obscuring their ordinary outcome.
Baseline-failing tests are excluded and named in JSON, the rating says that its
evidence is limited to green tests, and the command still exits with pytest's
failure status. If no test passed, Wreath prints `not measured` instead of
manufacturing confidence. In the default `auto` mode, only the sampled mutation
lines are traced during the ordinary run and that coverage and pass/fail set
become the mutation baseline; the suite is not traversed a second time. A fresh
mutation interpreter plans and compiles the sampled controls while the ordinary
xdist workers are still busy. It does not redundantly collect the whole suite:
each fork imports only the completed green candidate it is handed.
As soon as a watched test completes green, one of those children may probe its
control while unrelated ordinary tests are still running. An early kill is
conclusive and awards gold immediately. An early pass is only speculative:
Wreath clears it and retries that mutant after the ordinary run atomically
seals the complete green baseline. The full semantic-test pool starts alone.
Once ten percent of the visible per-file blocks complete, one test worker yields
between cases and the first mutator starts. Live mutation and fuzz share a
three-slot background envelope: mutation alone may grow to three workers, while
automatic fuzz reserves one of those slots and leaves two mutators. Five test
workers remain through the slow tail. Mutation inherits all eight measured
slots only after baseline seal.
The fresh parent also avoids Python 3.14's unsafe shape of forking the
controller after xdist has run.

Use `--mutant-path`, `--mutant-tests`, `--mutant-operator`, and
`--mutant-only` to scope it; `--mutant-timeout`, `--mutant-max-candidates`, and
`--mutant-pytest-arg` control execution. `--mutant-workers auto` uses that
measured progressive split; choose `1` for a constrained machine or a larger
explicit value only after measuring that machine. `--mutant-maxfail 1` is the
default:
the first baseline-passing test that objects decides `killed`, so running more
candidate tests would add names without changing confidence. Set zero only when
you want every killer in JSON. Live probes may use the whole ordinary test
window, at background priority, and are stopped rather than allowed to extend
its tail. `--mutant-budget 50` caps only additional sealed-baseline execution
after pytest finishes. Reaching that ceiling leaves the remaining controls
undecided, prints `FINISH THE SAMPLE`, and does not fail a green pipeline;
increase it when you are ready to finish the answer. The JSON `live` object
records how many probes started and completed, how many were cancelled at the
seal, and when the first one started relative to mutation preparation.
The default native mutation engine records PEP 669 reachability during the
ordinary native run, compiles only candidate-bearing test files once, and lets
every forked mutant child use the inherited indexed C dispatcher. Candidate
completion is signalled by pidfd and returned through a pipe on the default
first-killer path, without polling or a result file per mutant. It refuses unsupported
candidate files instead of falling back. Use `--mutant-engine pytest` only as
the explicit oracle.
`--mutant-fail-on-survivor` turns a cleared
scope into a gate and treats both survived and unreached as findings. `--mutant
off` disables the default sample; `changed` and `full` deliberately take their
own instrumented baseline because their much larger watch set was explicitly
asked for.

Fuzzing is gated by mutation evidence but no longer waits for the whole mutation
phase. Once mutation-gold files reach five percent of the files that have passed
so far, the native controller yields one background slot to a fresh `-m ''`
pass over each gold file's exact killing tests and any explicitly marked fuzz
cases. That is the minimum evidence-bearing surface: every gold file advances,
without paying to rerun thousands of unrelated tests. Five test workers continue
alongside two mutators and that fuzz worker. A live fuzz batch that finishes
before seal is reused; an unfinished one is stopped rather than carrying a
one-worker serial tail forward. The final gold set is dispatched across the
full native worker pool after sealed mutation. Positive kill evidence advances
a file even when another control survives against it. `--fuzz auto` enables this whenever mutation is enabled and is the default;
`--fuzz off` stops after mutation, while explicit `--mutant off --fuzz on` is
refused because there is no evidence on which to base selection. `wreath fuzz`
forces the same test, mutation, and gated-fuzz pipeline. The main JSON report
gains a `fuzz` document with selected, passed, failed, batch, and live-start
evidence. Every mutation-gold file therefore finishes as a cyan star or a navy
fuzz-failure cross rather than remaining as a purple square.

The selected-control catalog is cached with timing history. Its key includes
source paths, nanosecond mtimes, sizes, sample size, and filters; changing source
invalidates it. This keeps steady-state confidence cheap without reusing a
selection after the population changed. `--no-history` makes the catalog cold
on purpose as part of a fully stateless run.

All arguments Wreath does not recognize are forwarded to pytest in their
original order, so markers, `-k`, `--maxfail`, plugin flags, and explicit `-n`
continue to work. The default worker count is `min(8, cpu_count)`; the current
13,297-test suite was 7% faster at eight workers than at six in repeated warm
runs on the six-core reference machine. Wreath's broader check task retains its
separately measured six-worker cap. `--workers 1` gives the serial process you
want for a debugger. If you pass pytest's own `-n`, it wins.

Pytest remains the explicit compatibility oracle and debugger target. The
routine engine is Wreath's native collector, fixture runtime, isolated workers,
and C vectorcall dispatcher; unsupported pytest semantics refuse at collection
instead of silently changing engines.

**Reference:** [`wreath.testing`](../reference/testing.md).
