# Porting from FastAPI with `wreath port`

You have a FastAPI application and a deadline. `wreath port` reads it — statically, never importing it — and writes back native wreath source: the declarations rewritten, the logic preserved, and everything it can't safely translate flagged for your eyes rather than silently guessed.

It is a codemod, not a magic wand. Its contract is simple and unwavering: **transpile declarations, copy logic, and never emit subtly-wrong code.**

## Run it

```bash
# Static analysis + a migration report — no files written, zero risk.
wreath port ./app --report-only

# Emit a translated copy into a sister tree (the safe default).
wreath port ./app --output ../app-wreath

# Rewrite in place (refuses on a dirty tree unless --force).
wreath port ./app --in-place

# Make the calls that reach past one file, instead of leaving a note.
wreath port ./app --output ../app-wreath --opinionated
```

`--report-only` is the place to start. It walks the tree, resolves which framework symbol every name refers to, classifies each construct, and prints counts plus a `file:line` list of what will translate cleanly and what needs review.

## `--opinionated`

The default emit stops at the edge of the file it is writing. Some translations
cannot: a query needs a session to run it, a session has to come from the
function's caller, and adding a parameter changes code the emitter is not looking
at. Left alone, those become a note — "add a `session` parameter and pass one in
from each caller".

`--opinionated` makes those decisions for you. For a session, that means the
whole chain: the route handler declares `session: Annotated[Session, FromORM()]`,
which wreath fills in; every function between the handler and the query gains a
`session: Session`; every call along the way passes it; and the queries are
written out as `await session.fetch(Llama.select().where(…))` rather than
described. It also settles the smaller hedges — `extra="ignore"` is dropped,
because wreath always rejects unknown fields, and so is a route's
`include_in_schema=False`, because there is no per-route switch to carry it to.

Two things to know before you turn it on:

- **A method is matched by name.** Knowing that `repo.llamas_in(...)` is
  `LlamaRepository.llamas_in` needs type inference this tool does not have, so a
  name is followed only when it can mean one thing: defined exactly once in the
  tree, and not a name a built-in type also answers to. A repository method
  called `all` or `get` is therefore left alone.
- **Callers outside the tree you pointed at are yours.** Every function whose
  signature changed carries a note saying so.

## What it translates vs. annotates

The emitter rewrites **declarative** surfaces and copies **function bodies byte-for-byte**, inserting a `# TODO(wreath-port: … [rule])` line above anything it won't touch.

Translated automatically:

- `FastAPI()`/`APIRouter()` → `Wreath()`/`Router()`, the five method decorators, and `request: Request` inserted as the first handler parameter.
- `Query(20, ge=1, le=100)` → `Annotated[int, Query(minimum=1, maximum=100)] = 20` (the marker-as-default split, `ge`/`le` → `minimum`/`maximum`).
- `class X(BaseModel)` → `@dataclass` (pydantic v1 and v2); `= []` → `field(default_factory=list)`. A `Field(...)` holding only a default and documentation becomes the plain default — `Field(default=3, description="…")` is `= 3`, `Field(default_factory=list)` is `= field(default_factory=list)`, and a required `Field(...)` leaves a bare annotation. A class that declares a required field *after* a defaulted one becomes `@dataclass(kw_only=True)`: pydantic ignores declaration order and a dataclass refuses to be built, and the failure is at import time where neither `ast.parse` nor `compile` would have caught it.
- `fastapi.responses.<X>` → `wreath.response.<X>`, with `content=` and `status_code=` renamed to what wreath calls them; `status.HTTP_404_NOT_FOUND` → `404`; `jsonable_encoder(x)` → `x`; `arrow.utcnow()` → `temporal.now()`; `TTLCache(maxsize=…)` → `BoundedCache(max_entries=…)`. Each dead import goes with the last use of it.
- A `.objects.` chain inside a route handler, when every lookup carries across: `Llama.objects.filter(herd=h).order_by("-age").all()` → `await session.fetch(Llama.select().where(Llama.herd == h).order_by(Llama.age.desc()))`, with the session parameter added to the handler. Outside a handler this needs `--opinionated` (see below).
- `class X(ormar.Model)` → `class X(Model, table="…")` with per-column type mapping; a `ForeignKey` splits into a `column(<pk-type>, references=…)` plus a `relationship(…)`, with the FK type **inferred from the referenced model's primary key**.
- `HTTPException(status_code=404, …)` → `raise NotFound(…)`; FastAPI's
  `add_middleware(CORSMiddleware, …)` → first-class `HttpPolicy(cors=CorsPolicy(…))`.
- A model bound from a form (`as_form`) → `Annotated[Model, Form()]`.

Annotated for you (a real wreath target exists, but the rewrite isn't statically safe):

- ORM `.objects.` query chains, custom `BaseHTTPMiddleware`, lifespan context managers, bespoke auth bodies — each pointed at the corresponding built-in (`db.lock`, `app.jobs`, `oidc_provider`, …).

Left untouched and flagged `unsupported` (no wreath equivalent — keep the library): DynamoDB, OR-Tools, cloud SDKs, pandas/numpy analysis code.

## The ORM query chain, verb by verb

In a mature ormar codebase `.objects.` is not one construct among many — it is
*the* construct, easily a third of every framework token in the tree.

Reporting all of that as one line ("rewrite by hand") tells you the size of the
job and nothing about its shape, so the report classifies each chain by its
verb:

| ormar | wreath | notes |
| --- | --- | --- |
| `.objects.filter(**kw)` | `Model.select().where(...)` + `session.fetch()` | `__gte`, `__in`, `__isnull` and the pattern lookups all carry across; a relation lookup does not |
| `.objects.get_or_none(**kw)` | `await session.fetch_one(...)` | same contract, `None` on no match |
| `.objects.get(pk)` | `await session.get(Model, pk)` | **ormar raises `NoMatch`, wreath returns `None`** — port the miss branch |
| `.objects.create(**values)` | `session.add(Model(**values))` + `flush()` | |
| `.objects.select_related('rel')` | `.include(Model.rel.selectin())` | wreath never lazy-loads: a forgotten include *raises* rather than N+1-ing. `select_all()` names no relations, and wreath has no such switch |
| `.objects.order_by('-col')` | `.order_by(Model.col.desc())` | a trailing `.first()` becomes `fetch_one(...limit(1))` — the objection to `first()` is an *unordered* first, which this is not |
| `.objects.count()` / `.exists()` | `await session.count(...)` | |
| `.objects.get_or_create(...)` | — | a read-then-write race in one call; write the upsert explicitly |

**The verdict depends on the arguments, not just the verb.** `filter(id=x)` is
`translated`: every keyword maps to a wreath predicate with the value carried
across untouched. So is `filter(name__icontains=x)` — ormar's own `icontains`
compiles to `ILIKE '%' || value || '%'`, so writing that is a translation of what
you wrote and not a guess about it. `filter(ranch__slug=x)` is not, though the
join is *not* yours to write: `Model.ranch.slug` is a related column and wreath
plans the `INNER JOIN` for you. What the tool cannot do is resolve `ranch` to its
target model when that model is declared in another file, so it hands you the
rewrite rather than performing it. Same verb, different verdicts; sort the JSON
report by `rule_id` to get each list.

A `translated` query is **written out** wherever a session is in scope — inside a
route handler always, and everywhere else under `--opinionated`. Where it is not,
the note describes the target instead: the tag says the target is determined, not
that this file was the right place to change a signature.

## Things wreath grew an answer for

A porting tool ages in one specific way: a construct gets catalogued as "keep
the library", wreath later ships the thing, and the report goes on recommending
a dependency you could delete. These were re-pointed after measuring what a real
codebase actually imports:

- **`cachetools` TTL/LRU caches** → `wreath.cache`. Worth doing for more than
  parity: a `TTLCache(ttl=300)` is a guess about staleness, and
  `@cached(invalidate_on=[Llama])` clears on the committed write instead. Add
  `invalidate_across_workers(bus)` and that becomes fleet-wide.
- **`strawberry` / GraphQL servers** → `wreath.graphql`. Previously flagged
  `unsupported`; it isn't any more. Types derive from the ORM registry, so a
  `@strawberry.type` whose `strawberry.auto` fields *are* the model's columns is
  a deletion, and the report says so. Two things stop it being one, and the
  report names whichever applies rather than advising the delete anyway:
  a type listing **fewer** columns than the model is a deliberately narrowed
  surface, and wreath's exposure is per model rather than per field — deleting
  the class would publish the rest. And strawberry camel-cases field names by
  default while wreath emits the column name verbatim, so a `fleece_kg` field is
  `fleeceKg` today and `fleece_kg` after the port: a rename every client sees.
- **`httpx.AsyncClient`** → `app.http_client(...)`, a managed pool with lifespan
  start/drain.
- **FastAPI's `TestClient`** → `wreath.testing.TestClient`, which is **async**.
  And `dependency_overrides`, which in a real suite is overwhelmingly used to
  swap the auth dependency, is usually `TestClient.acting_as("bo", roles=[...])`.
- **`fastapi.status` constants, response classes, `jsonable_encoder`** → direct
  equivalents (or, for `jsonable_encoder`, nothing at all — wreath's codec
  serializes dataclasses and ORM rows directly).

- **`arrow`** → `wreath.temporal`, which is a rename per call:
  `arrow.utcnow()`/`arrow.now()` become `temporal.now()`, `arrow.get(s)` becomes
  `temporal.parse(s)`, and `.humanize()` becomes `temporal.relative(value)`. An
  `Instant` is a `datetime` subclass, so it stores, compares, and serializes
  with no conversion at the edges — and it refuses to be naive, which is the bug
  arrow's implicit UTC hides. A calendar `shift(months=)` is the exception and
  reports separately: months are not a fixed number of seconds, and temporal
  will not pretend otherwise.

- **A `boto3` S3 client** → `wreath.objects`. The verdict now reads the *service
  name*: `boto3.client("s3")` has a target (`S3ObjectStore`, `ObjectPath`,
  `zip_stream`), and `boto3.client("dynamodb")` still has none. One import, two
  answers — reporting them alike told you to keep a dependency you can delete,
  or to look for a replacement that does not exist.
- **A hand-rolled HMAC webhook verify** → `wreath.webhooks`'
  `HMACWebhookVerifier`. Worth more than a rename: the hand-rolled form compares
  the digest and stops there, so a captured request replays forever. The shipped
  verifier also checks the timestamp against a replay window and refuses an
  envelope whose relay path it has already seen.
- **An Alembic revision that calls `op.get_bind()`** → a deferred data
  migration. This one said "wreath has no online/deferred backfill yet
  (designed, not shipped): keep it in Alembic" until the day it shipped. A
  `Recode(Model.col, mapping={...})` beside the model converts rows in chunks
  while the application serves, and `wreath migrations check` refuses a later
  migration that narrows the column before the pass has published. The mapping
  is still yours to write, which is why it is `needs-review` rather than
  automatic.

This is a catalog that gets re-audited when a subsystem lands. `arrow` was
`needs-review` with the note "a native temporal layer is designed but NOT
shipped — do not wait for it", which was true when it was written and became
misleading the day `wreath.temporal` merged.

A `BaseHTTPMiddleware` subclass is the other shape that had gone missing, for a
different reason: the rule fired where middleware was *wired up*
(`add_middleware(...)`) and never where it was *written*, so a middleware living
in its own module — the ordinary layout — produced no finding at all.

## Alembic revisions, sorted by risk

Wreath's migration source of truth is the ORM image, not a chain of scripts, so
most of an Alembic revision has no wreath counterpart to *write* — which is a
translated verdict, not an outstanding task. The report sorts the operations by
which ones that is actually true of:

- **ordinary DDL** (`add_column`, `create_index`, `drop_table`,
  `alter_column(nullable=)`, …) — `translated`. `wreath migrations detect` reads
  these off the model change and `generate` emits the artifact. Confirm the
  ported model declares the end state (wreath is `NOT NULL` by default) and the
  revision is the generator's to own. The verdict is per *operation*, not per
  verb: it holds only while every argument stays inside what detection covers —
  tables, columns, primary keys, unique constraints, foreign keys and btree
  indexes.
- **renames** (`rename_table`, `alter_column(new_column_name=)`) — the one
  ordinary-looking operation whose derived form is *wrong* rather than absent. An
  image differ sees one object dropped and another created, and that moves no
  data. Keep the revision, or rename in the database first so `detect` sees a
  matching image.
- **indexes wreath does not model yet** (expression, partial, covering,
  non-btree) — emitted as `MANUAL` operations that cannot be applied, and
  therefore cannot be downgraded either. Keep them in Alembic.
- **types and constraints with no model attribute** (`sa.Numeric`, `sa.Enum`, a
  check constraint, a bare `drop_constraint` whose kind the call does not say) —
  the generator has nothing to derive them from. Decide whether the object moves
  onto the model or the table stays in Alembic.
- **raw SQL** (`op.execute(...)`) — no generator can infer it. Keep it in
  Alembic.
- **data migrations** (`op.get_bind()`) — this revision rewrites *rows*. It is
  the one that takes an hour on a large table and holds up a deploy, so it is
  called out separately rather than counted as one more DDL op.

## Splits by shape, not by name

Three more verdicts read the construct instead of its name, the way the query
rules read a `filter()`'s arguments (and so does the `@strawberry.type` rule
above). In each case the half that stays under review is the point:

- **`BaseSettings`** is `translated` when every field is a `str`/`int`/`float`/
  `bool` with a literal default or none at all: the target is `load_env` plus a
  dataclass, and the report hands over the `required_env=[...]` list the
  no-default fields imply, with any literal `env_prefix` already applied. One
  validator, container type, computed default or sub-group and the class waits
  for a human — a `list[str]` field is a JSON decode pydantic-settings did for
  you and `load_env` will not.
- **`status_code=`** has no slot on a wreath route; the status lives on the
  response. When the handler's single `return` is a literal, wreath's own
  coercion already decides which response class that is (dict/list/number →
  `JSONResponse`, str → `TextResponse`), so wrapping it changes the status and
  nothing else. When the return is a name or a call, the runtime type decides —
  and a dataclass is not JSON-serializable in wreath at all
  (`dataclasses.asdict` is the step), so the report asks rather than guesses.
  `status_code=204` with no return is `return Response(status=204)`; with a
  return it is a contradiction the source got away with.
- **An `@asynccontextmanager` lifespan** splits into `on_startup`/`on_shutdown`
  when a bare top-level `yield` partitions the body. If a name made before the
  yield is used after it, the message names that name: the halves are separate
  functions now, so it needs a home on `app.state`. An `asynccontextmanager`
  that is *not* the app's lifespan — a lock or connection helper — is not
  reported at all, because it needs no porting.

## What it walks, and what it refuses to

A real checkout is not just your application. It has a virtualenv in it, a
`node_modules` next door, a `build/` holding a second copy of the source, and a
`.git` full of nothing you wrote. Walking those as application code inflates the
denominator with libraries you are not porting — and, with an environment
present, drags a few thousand unrelated files into a run you did not ask for.

So the walk prunes, on two bases:

- **A virtualenv is found by its marker, `pyvenv.cfg` — not by its name.**
  `.venv` is a convention, not a rule, and an environment called `herd-env` is
  still an environment. Any directory containing that file is skipped whole. The
  root you name is exempt: if you point `wreath port` *at* an environment, that
  was a decision.
- **Conventional infrastructure names**, so an environment with a missing or
  stale marker still falls out: anything beginning with `.` (`.git`, `.tox`,
  `.nox`, `.mypy_cache`, `.ruff_cache`, `.direnv`, `.idea`), plus `__pycache__`,
  `node_modules`, `site-packages`, `venv`, `build`, `dist`, and `*.egg-info`.

The prune is on the *name of a directory*, never on a pattern that could match a
module you wrote: `env/`, `builders/` and `distribution/` are yours and are
walked. Symbolic links to directories are not followed, so a link out of the tree
cannot widen the run past the path you named.

## One bad file is a line in the report, not the end of the run

Over three thousand files there is always one that will not read: a broken
symlink, a permission bit, a file deleted between the walk and the read, a
latin-1 module, a fixture named `.py` that is really a blob, a generated
expression nested past the parser's stack budget. None of those end the run any
more — each takes its own file out and leaves the rest in.

But a silently skipped file is indistinguishable from a file with nothing in it,
and that is precisely how a coverage number becomes a lie. So every skip is
named, with a stable reason code, in its own report section and under `skipped`
in the JSON:

```
## Files that could not be analyzed

- `unreadable` app/legacy/link.py — [Errno 2] No such file or directory
- `undecodable` app/fixtures/latin1.py — 'utf-8' codec can't decode byte 0xe9…
- `syntax-error` app/scripts/py2_import.py — Missing parentheses in call to 'print'
- `too-deep` app/generated/tables.py — Stack overflow during compilation
```

The reasons are `unreadable` (an `OSError` — permissions, a dangling link, a
vanished file, a directory that would not list), `undecodable` (not UTF-8),
`syntax-error`, `invalid-source`, `too-deep`, and `out-of-memory`. Interrupting
the run still interrupts it: `KeyboardInterrupt` is not caught.

**Skipped files sit outside the coverage fraction entirely** — they contribute to
neither its numerator nor its denominator, because a file that could not be
parsed has no constructs to classify and inventing a verdict for it would be a
guess. Coverage therefore answers "of what I could read, how much carries
across", and the `files_analyzed` and `skipped` counts at the top of the report
say how much of your tree that sentence covers. Read them together; a 92% over
40 of your 3009 files is not a 92%.

## Idempotent, re-runnable

Every emitted file carries a provenance header with a hash of its source and of its own output. Re-running skips unchanged sources and **refuses to clobber a file you've hand-edited** (unless `--force`), so a port can be an iterative conversation, not a one-shot leap.

Emitting follows the same rule as analysing: **a source it cannot read is
recorded and stepped over, not fatal.** Those land in `PortResult.failed` (with
the same reason codes as the report) and print as `FAILED` lines, so a run over a
tree with one broken symlink still ports the other three thousand files.

A destination it cannot *write* is the opposite — that one is fatal, and
deliberately so. An unreadable source costs you one file; an unwritable
destination means the output tree itself is wrong (a full disk, a read-only
mount, a bad `--output`), every remaining file would hit the same wall, and
carrying on would hand you a half-written tree that looks like a complete port.

`failed` and `skipped` stay separate for the same reason. A **skip** is a
success — the output was already current, or you had hand-edited it and refusing
to overwrite was correct. A **failure** never reached the output at all. Fold
them together and "your tree is already ported" becomes indistinguishable from
"a third of your tree never made it".

## Coverage is a diagnostic, not a target

The report includes a coverage number — currently around **0.78** of constructs auto-translated across the representative apps.

**When nothing was recognized, coverage is `n/a`, not 100%.** An empty
denominator used to render as a perfect score, which meant the tool printed its
most flattering number in the one situation where it had failed hardest: point it
at a tree it understands nothing of — the wrong directory, a Django app, a
checkout that did not finish — and it congratulated you. `coverage_overall()`
and `coverage(category)` now return `None` there, the JSON carries `null`, and
the summary line says so in words. If you consume the JSON, handle the null; a
`null` that crashes your dashboard is a better failure than a `1.0` that does not.

It once went *down*, when the catalog learned to recognise more of what a real codebase contains, and that was the right direction: the denominator grew because the tool stopped being silent about caches, migrations, GraphQL, and the test suite. It has since gone up by reading constructs it already recognised more closely, which is also the right direction — and once by *removing* a verdict, when the catalog stopped calling every single-`return` handler's `status_code=` translatable, because for a handler returning a DTO the rewrite it promised produced code that raised on the first request. Do not chase it toward 1.0. A meaningful fraction of any real app (queries, domain logic, third-party integrations) is *correctly* left for a human; the tool's value is that this remainder is **precisely enumerated up front** instead of discovered at runtime. A lower, honest number beats a higher, hopeful one.

## What the exit code says

CI reads the process, not the markdown. `wreath port` uses the same three codes
as the rest of the CLI — the ones `wreath docs` and `wreath inspect` already use.

| code | meaning |
| --- | --- |
| `0` | It ran and left you nothing to do. Everything recognised translates, and every file was read. |
| `1` | It ran and left work: **unsupported constructs, files it could not read, or both.** The report names which. |
| `2` | It never ran over anything — no Python file was analysed, or the source path does not exist. |

**An app that has already been ported exits `0`.** It recognises nothing, because
there is no FastAPI left in it — and that is a successful run with nothing to do,
not a failure. If you re-run `wreath port` as a regression check, green is the
answer you want and the answer you get. What separates that from a wrong
directory is `files_analyzed`: fifty-two files read and nothing recognised is a
finished port; zero files read is a path problem, and that is the `2`.

The one consequence worth knowing before you wire this into a pipeline: a tree
with **one** unreadable file exits `1`, even if the other three thousand were
fine. That is deliberate. A file nobody could read is a file nobody has ported,
and it belongs on the same list as a construct nobody can translate.

Skipped files share `1` with unsupported constructs rather than getting a code of
their own, so read the report to tell them apart — and read it anyway when
anything was skipped, because **an unsupported count taken over a partial tree is
a lower bound rather than a count**. The summary line, the skipped section, and
`files_analyzed` in the JSON all say so.

Emit mode (`--output` / `--in-place`) reads the same way: a source that could not
be read is work remaining (`1`), a tree with nothing to emit at all is `2`, and a
re-run over an unchanged tree — where every file is skipped because it is already
correct — is `0`.

## The report as a checklist

`--report-only` emits both a human summary and `wreath-port-report.json` (`translated` / `needs-review` / `unsupported`, each with `file:line` and a rule id). Pair it with a `grep TODO(wreath-port` over the emitted tree and you have a complete, deduplicated worklist for the parts that are yours to finish.
