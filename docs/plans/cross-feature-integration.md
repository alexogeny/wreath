# Prescriptive plan: what the four July features are worth together

Status: **all six items implemented** (July 2026). Items 1 and 4 were defects
with reproductions; items 2, 3 and 5 were compositions that already worked and
were documented nowhere. What each item's section describes as "what is missing"
is now present; the two places where implementation departed from the plan are
recorded in the item that departed — item 2's `Access` combinator refuses
`public()` and `deny()`, and item 6 collided with prose written after the plan
was.

The rejected seams below stay rejected, and are the half of this document that
is still load-bearing: they are the reason nobody needs to re-derive them.

Related material:

- `tests/test_cross_subsystem.py` — the proving tests written for this plan. Eleven
  tests, all green, joining MCP to second factors and to Postgres retrieval.
- `docs/plans/native-mcp-server.md`, `docs/plans/second-factors-totp-webauthn.md`,
  `docs/plans/postgres-retrieval-vector-fulltext.md`, `docs/plans/docs-capability-map.md`
  — the four features this is about.
- `src/wreath/_auth/requirements.py` — `AuthRequirement`, the type that turns out
  to be the whole seam between three of them.

## The problem

Four substantial features landed on one day, each built by someone who could not
see the other three: an MCP server, second factors, Postgres-native retrieval,
and a generated capability map. Each is internally coherent and separately
documented. Nothing anywhere joins any two of them.

That matters more than it sounds. `docs/guides/mcp.md` runs to 784 lines and
contains the strings "second factor", "vector", "search" and "retrieval" exactly
zero times. No guide outside `docs/guides/mcp.md` and `docs/reference/mcp.md`
mentions MCP at all. The five guides that would answer "how do I expose my
application's search to a model, safely" — `mcp.md`, `hybrid-search.md`,
`vector-search.md`, `full-text-search.md`, `second-factors.md` — are five silos
with no link between them.

Where the composition *works*, nobody knows. Where a default is wrong because one
author could not see the other, nobody has looked.

## Goal

Name the small number of places where two of these features compound, prove the
ones that already work, fix the ones that are off by a line, and record the ones
that do not hold up so the next reader does not re-derive them.

## Non-goals

- **No new concepts.** Nothing here introduces a type, a decorator or a protocol.
  The largest change proposed is one keyword argument.
- **No runtime dependency.** ADR 0002 stands.
- **No embedding model, no chunking, no ingestion pipeline.** Wreath stores
  vectors and ranks by them; producing them is the application's job and stays
  that way.
- **No themed vocabulary.** The brand is poetic and the API is literal. Nothing
  below renames a technical term.
- **Not a survey.** Seven candidate seams were examined. Three are kept, one is
  split, three are rejected below with the evidence that killed them.

---

## Item 1 — Resolve the caller before the throttle and the audit marker

**Rank: first.** It is the only item where something a developer explicitly wired
does not do what it says.

### What exists today

`MCPServer._tools_call` computes the caller at `src/wreath/_mcp/server.py:1190`:

```python
identity = request.identity
principal = None if identity is None else identity.id
```

`request.identity` is populated by `_authenticate` — but `_authenticate` returns
immediately when `self._auth is None`, i.e. whenever the endpoint carries no
`MCPAuth`. On such an endpoint the identity is resolved *lazily*, inside
`_authorize` → `_identify` (`server.py:1802`), which runs the application's own
backend on the first declaration that needs it.

`_authorize` runs at line 1253. The `principal` is read at 1190 and the rate
limit is charged at 1208. Both are before it.

An endpoint with no `MCPAuth` and an `app.configure_auth(...)` backend is not an
exotic configuration — it is the configuration `expose_routes` exists for, and
the one `_identify`'s own docstring describes as "what makes `expose_routes` work
at all".

### What is wrong

**A per-tool rate limit is not enforced.** The bucket key falls back to
`session.id` (`server.py:1740`), and `initialize` mints a session for anyone who
asks. Reproduction (scratch script, not committed):

```
call 1 on session A: allowed
call 2 on session A: refused
call on fresh session 0..4: ALLOWED, ALLOWED, ALLOWED, ALLOWED, ALLOWED
handler ran 6 times; the declared ceiling was 1 per hour
mcp.throttled = 1  mcp.tool_calls = 6
```

`ToolRateLimit`'s docstring is technically accurate — "keyed on the verified
subject when the endpoint carries `MCPAuth`, and on the session otherwise" — and
the consequence is not stated: a session is free, so a per-session ceiling is no
ceiling. `_mcp/limits.py`'s own module docstring says a model "will call the same
tool in a loop, open a session per turn", which is exactly the traffic that walks
through this.

**The audit trail names the wrong caller.** The `tools/call` marker records
`principal="anonymous"` for a caller the same request authenticated and
authorized. Observed while writing
`test_the_search_the_model_asked_for_is_on_the_audit_trail`: the tool was gated on
`Note::search`, the Cedar decision was made against `User::ada`, and the marker
says `anonymous`. `_mcp/record.py` describes this field as "the subject the
caller's own token asserts", and it exists so an operator can answer "on whose
behalf" six months later. On this path it answers "nobody".

`ToolContext.identity` carries the same stale value, and is not *dishonest* — its
docstring says "when the endpoint carries `MCPAuth`" — but it is the reason a
tool handler that reads `request.state.mcp.identity` to decide what to return
sees `None` for a caller the server just authorized.

### What is missing

Resolve the identity before the throttle and the marker, without giving up the
laziness `_identify` was written for. In `_tools_call`, before line 1190:

```python
if tool.limiter is not None or tool.requirement.access_level > 0:
    await self._identify(request)
identity = request.identity
```

An ungated, unlimited tool still never runs the backend, which is the property
`_identify` protects. A gated or bounded one resolves once, and `_identify`
already memoizes onto the request, so `_authorize` does no extra work.

`_sampling` and `_elicitation` (`server.py:1500`, `1595`) then follow for free:
they read `context.identity`, which `_tools_call` set from the same stale value
at line 1245, and they charge the same bucket. Fixing the source fixes all three,
and makes `ToolContext.identity` mean "the caller" on every path — at which point
its docstring's "when the endpoint carries `MCPAuth`" qualifier can go.

### Cost

Three lines plus two mirrors, and two tests: one that a fresh session does not
reset a tool's ceiling, one that the marker names the caller on an endpoint with
no `MCPAuth`. Half a day including the guide sentence that should have been in
`ToolRateLimit`'s docstring all along.

---

## Item 2 — Step-up already reaches MCP. Say so, and let a declared tool ask for it

### What exists today — and it works

This is candidate 1 from the brief, and it holds up. `Tool.requirement` is a whole
`AuthRequirement`; `wreath.auth.second_factor` writes `AuthRequirement.second_factor`;
`expose_routes` merges the route's requirement into the tool unchanged; and
`MCPServer._authorize` checks it at `server.py:1767` through the same
`second_factor_age` the HTTP pipeline uses at `app.py:2601`.

Proved, in `tests/test_cross_subsystem.py`:

- `test_a_step_up_route_carries_its_window_onto_the_tool` — the compiled
  requirement, statically.
- `test_a_caller_who_never_proved_a_factor_cannot_call_the_tool` — refused, and
  counted in `unauthorized_calls` rather than `tool_errors`.
- `test_a_caller_who_proved_a_factor_recently_may_call_it`
- `test_a_factor_proved_too_long_ago_is_refused` — recency, which is the point.
- `test_the_stamp_wreath_users_writes_is_the_claim_the_tool_reads` — the whole
  path: `wreath.users` stamps `second_factor_at` on the session principal,
  `SessionIdentityBackend` copies the principal into `Identity.claims`,
  `wreath._mcp.server` reads it back. Three modules that never mention each other,
  agreeing on one key, asserted nowhere until now.
- `test_a_cedar_gated_tool_sees_the_second_factor_age_in_its_context` — the more
  expressive half: `CedarAuthorizer`'s default context mapper publishes
  `second_factor_age`, and the MCP server runs policies through the same
  authorizer, so `when { context.second_factor_age <= 300 }` gates a tool with no
  MCP-specific code. The key is absent rather than zero when no factor was proved,
  so both `when` and `unless` shapes fail closed.

**Nobody could have known.** `src/wreath/mcp.py`'s module docstring says "an
exposed route keeps whatever `@authorize`, `@roles` or `@permissions` it was
behind" — a list that omits `@second_factor`, which is the most interesting member
of it. This is a framework that can say "the model may read, and the human must
re-prove a factor before the model may delete", and it says so nowhere.

### What is missing

**(a) A declared tool cannot ask for step-up.** `MCP.tool()` takes `action=`,
`resource=`, `rate_limit=`, `sampling=`, `elicitation=` and nothing that demands a
recent factor. Step-up reaches a tool only by exposing a route that already had
it. Pinned by `test_a_declared_tool_has_no_step_up_keyword_of_its_own` — deleted
in the change that added the keyword, and replaced by the four tests that assert
the keyword compiles, admits a fresh factor, and refuses an absent and a stale
one.

The change is one keyword threaded through `MCP.tool` → `build_tool`, merged the
way `action=` already is:

```python
declared = NO_REQUIREMENT if action is None else policy_requirement(action, resource)
if second_factor is not None:
    declared = replace(declared, authenticated=True, second_factor=second_factor)
```

Ten lines with the docstring. Worth doing because the natural way to write "this
tool deletes things, ask for the code again" should not require inventing a route
to hang it on.

**(b) `Access` has no step-up either.** This is candidate 3 from the brief, and it
is the same shape one layer down. `wreath.crud` generates `DELETE /{id}`; `Access`
has `public`, `authenticated`, `roles`, `permissions`, `cedar`, `deny`, and no way
to say "and prove a factor". Because `Access` is a single `kind`, the right form is
not a seventh factory — it is a field plus a combinator, so step-up composes with
the rule rather than replacing it:

```python
authorize={"delete": Access.roles("admin").within(300)}
```

`_apply_requirement` then calls `add_second_factor` alongside whatever the kind
produced. `AuthRequirement` already merges strictest-window-wins, so nothing new
is needed underneath. Roughly fifteen lines.

**Where (a) and (b) departed from this, when they were written.** Both refuse a
window that is zero or negative, the way `@second_factor` already does — a
window nobody can satisfy is a typo, and `deny()` is how "never" is spelled.
`Access.within` additionally refuses `public()` and `deny()`: `add_second_factor`
sets `authenticated=True`, so a `public` rule carrying a window would silently
stop being public, which is the kind of quiet promotion this codebase writes
`ValueError`s about. And in `build_tool` the keyword is applied *before* the
merge with an exposed route's own requirement, not after, so a declaration can
never relax a window the route already asked for — the plan's snippet above sits
at the line where that ordering is decided, and the pinning test for it is
`test_an_exposed_route_keeps_the_shorter_window_the_tool_declares`.

**(c) One-line divergence between the two enforcers.** `AuthRequirement.access_level`
counts `authenticated`, roles, permissions and policies, and not `second_factor`.
A bare `AuthRequirement(second_factor=300.0)` therefore has `access_level == 0`,
which makes the MCP `_authorize` early-return `None` at `server.py:1756` and skip
the check entirely — while `app._authorize_request` still enforces it. Verified:

```
access_level for a bare second_factor requirement: 0
mcp _authorize would early-return None: True
app _authorize_request would still check it: True
```

Not reachable through the public decorators, because `add_second_factor` always
sets `authenticated=True`. It is still two implementations of one rule
disagreeing, in the module whose docstring says "one decision for tools,
resources, prompts and route-derived tools alike". Fix it in `access_level`:

```python
if self.authenticated or self.second_factor is not None or self.role_checks or ...
```

### Cost

(a) ten lines, (b) fifteen, (c) one, plus the docstring in `mcp.py` that should
name `@second_factor` in its list, plus a short section in `docs/guides/mcp.md`
and a cross-link from `docs/guides/second-factors.md`. One day for all of it.

---

## Item 3 — The retrieval tool: write the example that both halves imply

This is candidate 2 from the brief, and it needs **no code at all**.

### What exists today — and it works

A model-facing retrieval tool over the application's own Postgres is the most
common application shape of 2026, and both halves are first-party. Proved end to
end against a live PostgreSQL with pgvector, in `tests/test_cross_subsystem.py`:

- `test_a_hybrid_search_is_reachable_as_one_mcp_tool` — a `Queries` class with a
  vector search, a `tsvector` search and a `fuse(...)` of the two, called through
  `tools/call`, returning the hand-computed fused order `[2, 1, 4, 3]` that
  neither half produces alone.
- `test_the_retrieval_tool_is_behind_the_same_cedar_decision_as_a_route` — gated
  on a Cedar action, with a second tool on the same server that the policy
  refuses, so the assertion is about the decision and not about the wiring.
- `test_the_retrieval_tool_can_be_bounded_per_caller` — one `rate_limit=` keyword
  (see item 1 for the caveat that makes this weaker than it looks).
- `test_the_search_the_model_asked_for_is_on_the_audit_trail` — the search terms
  ride the Flight Recorder's canonical line under `mcp.arg.`, fingerprinted rather
  than raw, so an operator can ask "was this the same query" without publishing
  what somebody searched for.

The handler is nine lines and declares nothing about authorization, throttling or
auditing. Every one of those is a property of the declaration.

### What is missing

The example. A section in `docs/guides/mcp.md` and a reciprocal link from
`docs/guides/hybrid-search.md`, showing the tool above in full, and saying the
thing that is true and unusual: this is a retrieval tool that is Cedar-gated,
per-caller bounded, and on an audit trail, with no vector database, no separate
search service, and no framework outside the one already serving the application.

### Cost

Zero code. One guide section, one cross-link, and the tests already exist. Do this
one first if the goal is value per hour; do item 1 first if the goal is
correctness, because the guide should not advertise a `rate_limit=` that item 1
has not yet made real.

---

## Item 4 — Generated CRUD's defaults are wrong for a model with retrieval columns

`wreath.crud` was written before `Vector` and `TsVector` existed. Its defaults —
"every column that does not look like a secret" — were correct for the column
types that existed then.

### What is wrong

Reproduced live against PostgreSQL with a model carrying `embedding: Vector(3)`
and `search: TsVector(sources=("title",))`:

```
GET /doc -> 200
  {"items": [{"id": 1, "title": "llamas", "embedding": [1.0, 0.0, 0.0],
              "search": "000000016c6c616d610000010001"}], "page": 1, "size": 20}
POST with a generated column -> 422
  {"error": "a tsvector column is generated: PostgreSQL derives it from its
             source columns on every write, so assigning it would be discarded"}
PATCH the embedding -> 200
  {"id": 1, "title": "llamas", "embedding": [0.0, 0.0, 1.0], ...}
```

Three separate problems, in descending order of seriousness:

1. **The retrieval index is client-writable.** `PATCH /doc/1 {"embedding": [...]}`
   succeeds. For an application whose search is semantic, anyone who may edit a
   row may place it at the top of every query — and unlike editing the text, this
   leaves the visible content untouched. `writable_fields` (`crud.py:335`) excludes
   the primary key, `readonly=` and sensitive names; a vector column is none of
   those.
2. **A generated column is offered as writable and then rejects the write.** The
   primary key and `readonly=` columns are *silently dropped* from the body, which
   `crud_router`'s own docstring states as the rule. A generated column instead
   reaches the ORM and produces a 422 carrying an ORM-internal message. `Column`
   already exposes `generated` (`orm/fields.py:192`), so the fix is one clause.
3. **Both columns are serialized into every response.** A hex-encoded `tsvector`
   is noise by construction — it is derived from columns already in the same
   payload. With a realistic `Vector(1536)`, a default page of twenty rows carries
   about thirty thousand floats nobody asked for.

### What is missing

In `crud_router`:

- Drop generated columns from `writable_fields`, unconditionally. They can never
  be written; offering them is a bug in every case.
- Drop vector and generated-tsvector columns from `writable_fields` and from
  `output_fields` by default, with `expose=` naming them back — the same escape
  hatch the sensitive-name deny-list already uses. This widens `expose=` from
  "sensitive by name" to "withheld by default", which its docstring can carry.

The judgement call is whether an application may legitimately `POST` an embedding.
Some can — a client that computed the vector itself. That is what `expose=` is
for, and it makes the act auditable, which is the property the module already
argues for at length.

### Cost

About ten lines in `crud_router`, plus tests, plus one paragraph in
`docs/guides/crud.md`. Half a day. It is a behaviour change for any application
already relying on the current defaults, which is why it is item 4 and not item 1
— but that population is one day old.

---

## Item 5 — Pagination's default allow-list admits vector and tsvector columns

### What is wrong

`sortable_fields(model)` returns every column name, and `apply_sort` /
`apply_filters` default their allow-list to exactly that. Verified against a live
database:

```
sortable_fields: ('id', 'title', 'embedding', 'search')
  ?sort=embedding:  OK, 2 rows
  ?sort=-embedding: OK, 2 rows
  ?sort=search:     OK, 2 rows
```

pgvector gives `vector` a btree opclass, so `ORDER BY embedding` is valid SQL and
runs — as a full sort of the table on values that are kilobytes each, with no
index that can serve it, on a query string an anonymous caller controls. That is
the same class of request-triggered cost as the deep `OFFSET` that `MAX_PAGE`
exists to bound, twenty lines away in the same module.

### What is missing

Exclude non-scalar retrieval columns from the default allow-list. The wrinkle is
that `sortable_fields` is public and its docstring promises "every column name",
so changing it in place changes a documented contract. Two shapes:

- Filter inside `apply_sort` / `apply_filters` when no explicit `allow=` was given,
  and leave `sortable_fields` alone. Smallest blast radius; leaves a caller who
  passes `allow=sortable_fields(Model)` still exposed.
- Change `sortable_fields` and its docstring to "every column a caller may sort or
  filter by", which is what every call site already means by it.

The second is more honest and the docstring change is the whole cost.

### Cost

Five lines and a docstring, plus a test per column type. An hour.

---

## Item 6 — Give the capability map the vocabulary a retrieval reader arrives with

### What exists today

The map is generated from `docs/agents/manifest.json`, and all four July features
updated it properly — `mcp` names `mcp`/`fastmcp`/`fastapi-mcp`, `users` names
`pyotp`/`webauthn`/`fido2`, `orm` names `pgvector`. There is no gap in the
mechanism and no stale row. This item is small on purpose.

### What is missing

`replaces` is described as "the distribution names a reader would recognise", and
the reverse index exists so that somebody who arrives knowing a package name finds
the page. Somebody arriving with a retrieval problem does not type `pgvector` —
they type `chromadb`, `qdrant-client`, `pinecone-client`, or `weaviate-client`.
None of those is anywhere in the manifest, and `queries` names only `aiosql`.

Adding them to `orm.replaces` is honest: for an application already on PostgreSQL,
storing vectors in the same database and ranking with the same query planner is
precisely what those packages are usually bought for.

**`langchain` and `llama-index` must not be added.** Wreath does not do embeddings,
chunking, or model orchestration, and claiming otherwise would be the first
dishonest row on a page whose whole argument is that it is generated and therefore
true.

### What this collided with, when it was done

`docs/capabilities.md` grew its honest "What Wreath does not include" list after
this item was written, and that list already named `qdrant-client`, `chromadb`,
`pinecone` and `weaviate-client` under *"a dedicated search engine or vector
database"*. Landing item 6 unchanged would have put the same four packages in
both halves of one page — replaced in the table, still required three screens
down — which is the shape of dishonesty this item is otherwise careful about.

Resolved by keeping the `replaces` addition and narrowing the prose: the closing
list is now about a search *engine* (`elasticsearch`, `opensearch-py`,
`meilisearch`, `typesense`), and it says in one sentence which claim the ORM row
is making — storing vectors and ranking by distance, in the database you already
run — and which it is not. `queries.replaces` was deliberately left at `aiosql`
alone; two rows claiming the same package is noise in a table read by scanning.

### Cost

One manifest edit, one paragraph rewritten on `capabilities.md`, and the docs
build. Twenty minutes plus the collision above.

---

## Seams examined and rejected

Recorded so the next reader does not re-derive them.

**Full-text search as a parameter on a generated list endpoint** (candidate 4).
Rejected: it is a feature wearing an integration costume. For `crud_router` to
answer `?q=llamas` it must learn which column is the `tsvector`, which text-search
configuration it was declared with, whether to rank by `ts_rank` or leave the
order alone, and what to do for a model that has no such column. That is four
decisions, none of them derivable, on a module whose entire argument is that its
defaults are safe because they are dull. An application that wants search on a
list endpoint writes a route and a `Queries` declaration, and that route is nine
lines — item 3's example is that route.

**Vector columns plus durable jobs or progress** (candidate 5). Rejected on the
code. Re-embedding on write needs a row-grained write notification, and
`src/wreath/_orm_events.py` is model-grained *by design*: "Row-grained
invalidation needs the cache to know which rows fed which response, which means
recording a read set per request — real bookkeeping on the hot path to save a few
cache misses on the cold one." A subscriber learns that `Note` was written and
never which note. Building the row-grained path to serve re-embedding would
reverse a decision made for the response cache on performance grounds, which is a
plan of its own and not an integration. The other half — an index build reported
through `wreath.progress` — is an application writing a job with a reporter, with
no framework gap to close; `wreath.jobs` already carries a `ProgressRegistry` and
`JobContext.report` already exists.

**MCP plus recording/replay** (candidate 6). Rejected: it does not work, and
making it work is disproportionate. `wreath.replay.recorded_request` refuses a
recording that holds more than one HTTP request — "the recording holds N bytes
past the first request's body, so the connection carried more than one request" —
and an MCP session is at minimum `initialize` followed by `tools/call`. The
whole-connection replayer, `replay_transport`, would replay the recorded
`mcp-session-id` header against a server that has never minted it. Making a
captured session replayable needs session-id rewriting across the whole
recording, i.e. protocol awareness inside a byte-level replayer that is
deliberately protocol-agnostic. The payoff is small: `TestClient` already drives
the MCP endpoint in-process, which is what a regression test wants, and
`tests/test_mcp_*.py` is eleven files of evidence that it is enough.

**MCP plus OpenAPI or typegen** (candidate 7). Rejected: the consumer of
`tools/list` is a model, and the response is already self-describing JSON Schema
derived from the same signatures OpenAPI reads. A TypeScript client for an MCP
server serves nobody who exists — the client is Claude, or an MCP host written by
someone else. The bridge that is worth having runs the other way, from routes to
tools, and `expose_routes` is it.

**MCP plus progress.** Rejected as already done, not as unwired.
`_tools_call` builds `ToolContext(progress=self._progress.reporter(task_id))` and
relays what a tool writes as `notifications/progress`, and `src/wreath/mcp.py`'s
module docstring already explains it, including that a `ProgressRegistry` given
the message bus carries a durable job's progress across workers. Nothing to add.

---

## Ordering

1. **Item 1** — a control that is off. Everything else can wait behind it, and
   item 3's guide should not advertise `rate_limit=` until it is real.
2. **Item 3** — zero code, tests already written, and the largest gap between what
   Wreath can do and what a reader can discover.
3. **Item 2** — the docstring line first (five minutes), then the two keywords.
4. **Item 4** — a behaviour change, but on a population that is one day old.
5. **Item 5**, then **item 6**.

## Tests

`tests/test_cross_subsystem.py` exists and is green: eleven tests, seven of which
need no database and four of which are `@pytest.mark.database` behind
`WREATH_TEST_POSTGRES_DSN`. It proves items 2 and 3. It deliberately does *not*
pin the defects in items 1, 4 and 5 — a passing test that asserts wrong behaviour
is a trap for whoever fixes it — with one exception,
`test_a_declared_tool_has_no_step_up_keyword_of_its_own`, which pins a missing
keyword rather than a wrong answer and names the change that should delete it.
