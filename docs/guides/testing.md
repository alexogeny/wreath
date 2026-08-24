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

## See the suite while it runs

Once a suite is large enough, a row of dots hides the two things you usually
want to know: *what is still running?* and *where did the time go?* `wreath test`
runs pytest's collection, fixtures, plugins, capture, and tracebacks, but
puts every collected test file on a stable state-map tile:

```bash
wreath test
wreath test tests/auth/ -k refresh
wreath test --workers 1 --grid never tests/test_login.py
```

Queued files are gray, running files blue, passing files green, skipped or mixed
files orange, and failures red. During mutation confidence, candidate test files
are pink while a control is running, yellow after one of their tests kills a
mutant, and purple when a mutant survives those tests. Colour is categorical:
it never grades duration, because a file's percentile is relative to this one
run and is not a portable claim about that file. Distinct symbols keep the
states readable under `NO_COLOR`. Positions are sorted by path and do not move
while the run is in progress, so the grid becomes familiar rather than
reshuffling itself around the latest result.

The animation uses the terminal's alternate screen and restores the original
screen before pytest prints its normal failure and skip summaries. In CI, when
output is redirected, when `TERM=dumb`, or when `CI` is set, it prints one static
snapshot instead. `--grid always` and `--grid never` override that choice;
`NO_COLOR` keeps the outcome-specific symbols without ANSI colour.

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
wreath test                                         # 192-control auto sample
wreath test --mutant sample                         # the same 192 stable controls
wreath test --mutant-samples 48                     # a smaller sample; barely faster
wreath test --mutant sample --mutant-samples 384   # a larger confidence sample
wreath test --mutant changed --mutant-changed main # controls changed on this branch
wreath test --mutant full                           # the complete sweep
```

The sample is ranked by a stable hash across the whole eligible corpus. It is
not `wreath mutant --limit`, which takes the first controls in source order and
therefore spends a small budget at file heads. The final line gives an
actionable, colour-coded rating -- `SAMPLE WATCHED`, `REVIEW ASSERTIONS`, `ADD
COVERAGE`, or `FINISH THE SAMPLE` -- while keeping `killed`, `survived`,
`unreached`, and undecided controls separate. It does not average distinct
findings into a percentage. During this phase candidate test files turn pink;
a file turns yellow only after one of its tests kills the mutant, while a
survivor leaves its candidate files purple. A JSON test report gains the
complete `mutation` document, its structured rating, and
`verified_test_files` and `failed_mutation_test_files` for that same evidence.

Mutation uses only tests that passed in the ordinary run as eligible killers.
That keeps the evidence honest without throwing away the rest of a large run:
red files stay red, while green candidate files can turn pink and then yellow
or purple.
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
conclusive and awards the gold tile immediately. An early pass is only
speculative: Wreath clears the purple tile and retries that mutant after the
ordinary run atomically seals the complete green baseline. Up to three forked
mutant children run together by default, including for any sealed-baseline work
left at the tail. The fresh parent also avoids Python 3.14's unsafe shape of
forking the controller after xdist has run.

Use `--mutant-path`, `--mutant-tests`, `--mutant-operator`, and
`--mutant-only` to scope it; `--mutant-timeout`, `--mutant-max-candidates`, and
`--mutant-pytest-arg` control execution. `--mutant-workers auto` is capped at
three; choose `1` for a constrained machine or a larger explicit value only
after measuring that machine. `--mutant-maxfail 1` is the default:
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
`--mutant-fail-on-survivor` turns a cleared
scope into a gate and treats both survived and unreached as findings. `--mutant
off` disables the default sample; `changed` and `full` deliberately take their
own instrumented baseline because their much larger watch set was explicitly
asked for.

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

Pytest remains the execution engine deliberately. Reimplementing its fixture and
plugin model would create a second test dialect; the native opportunities are in
scheduling, worker transport, and rendering, and Wreath will move those only
after measurements show that they dominate a real run.

**Reference:** [`wreath.testing`](../reference/testing.md).
