# One name, one meaning

Wreath's public API is deliberately literal: `wreath.pagination` is
`src/wreath/pagination.py`, and you should be able to guess a path before
searching for it. That promise extends past module names to the verbs the
framework reuses across subsystems — and it is the verbs that had drifted.

This page is the index of the names that appear in more than one place, what
each one means, and which spelling to reach for when adding a subsystem. It
exists because four separate collection protocols and four separate "give me
your state" verbs had each grown a second spelling, and a reader could not tell
from a call site which one they were looking at.

## Getting a value out of an object

Four verbs. The distinction is **what is being asked**, not what comes back:

| Verb | Asks | Examples |
| --- | --- | --- |
| `stats()` | *how much has happened* — cumulative activity counters | `cache`, `jobs`, `messaging`, `streams`, `reactor`, `_logsink`, `_export`, `_recording_format`, `_mcp.server` |
| `snapshot()` | *what is there now* — the current contents or shape of something that moves | `queue`, `rooms`, `http_client`, `postgres`, `sync`, `_projector`, `_pure.kv`, `_pure.queue` |
| `as_dict()` | this value object, as a plain mapping | twenty-odd value types |
| `to_json()` | this value object, as JSON bytes | `objects.py`'s upload state, and nowhere else |

Return types deliberately do **not** appear in that table, because they are the
caller's convenience rather than the distinction. `cache.stats()` answers with a
typed `CacheStats` and `jobs.stats()` with a `dict[str, int]`; both are counters
and both are correctly named. A typed record is usually the better choice and is
never wrong — do not "normalize" one into a dict.

The two distinctions that are real, and were being got wrong:

* **`as_dict()` versus `to_json()`.** A verb naming a wire format must return
  that wire format. Nine `to_json()` methods returned a `dict` — in `_mutant`,
  `_audit`, `_port.ir`, `dup_scan` and `port_golden` — and one, `objects.py`'s
  upload state, returned actual JSON bytes. The nine are `as_dict()` now, and
  `to_json()` means bytes in the one place it survives.
* **`as_dict()` versus `to_dict()`.** `to_dict()` is not a serialization verb at
  all. It survives in exactly one place, `_sparsevec.SparseVector.to_dict()`,
  where it converts a sparse vector into an index-to-value mapping — a genuine
  type conversion, not a rendering.

The `stats`/`snapshot` line is subtle enough to be worth an example. A room
registry's member counts are a `snapshot()`: they go down as well as up, and
they describe the registry's present contents. A stream's started-versus-attached
tally is `stats()`: it only ever grows, and it describes history rather than
state.

## Claiming tables in the wreath schema

Four names, one mechanism, and the layering is the whole point. `wreath` owns a
schema and creates it during lifespan; the claims are collected by *asking*,
never from a hand-kept list, because a hand-kept list is one more place to
forget a new subsystem.

| Name | On | Shape |
| --- | --- | --- |
| `component()` | a registered subsystem the application holds | `() -> Component` |
| `schema_claim(name)` | a *declaration* that several subsystems reuse | `(str) -> Component` |
| `schema_owners` | a holder that delegates its tables elsewhere | property → tuple of candidates |
| `schema_database` | an owner the application never constructed | property → `Database` |

**`component()` takes no arguments.** `Wreath.schema_components` walks
`Wreath.schema_holders()` and calls `claim()` with nothing, so a `component()`
requiring a keyword is a `TypeError` waiting for the first application whose
`schema_owners` reaches it. `wreath.quota.QuotaRegistry.schema_owners` already
answers with store objects, so this is not hypothetical plumbing.
`tests/test_docs_api_shapes.py` asserts the arity from source.

**`schema_claim(name)` is the declaration layer.** One `wreath.store.Keyed`
shape backs sessions, rate limits and idempotency, and each needs its own
version marker and its own advisory lock, so the name has to come from the
caller. That is a different operation from "what do *you* claim", and it now has
a different name. `wreath.log.Log`, `wreath._passes.ledger` and
`wreath._series.settle` use the same spelling.

**Do not add a fourth walk.** `wreath.app.walk_claims` is the one traversal, and
`Wreath.schema_holders()` is the one holder list. Both used to exist in
triplicate — twice in `app.py` and once in `wreath.infra.inference` — and the
copies drifted: inference's list was missing the series and entity registries,
so a settled series' tables were absent from `wreath infra infer` for an
application that really did create them.

## Storage ports

Twenty protocols are the same idea: **a pluggable persistence or delivery port
the application supplies, with an in-memory default and usually a PostgreSQL
implementation.** They are listed here together because the suffix does not tell
you that, and an agent adding the twenty-first should copy one of these rather
than invent a twenty-first convention.

| Protocol | Methods | Module |
| --- | --- | --- |
| `OrganizationStore` | `organization`, `memberships`, `roles` | `organizations` |
| `MemberDirectory` | `members` | `organizations` |
| `UserStore` | `get_by_email`, `get_by_id`, `create`, `update` | `_userkit` |
| `EmailSender` | `send_verification`, `send_password_reset` | `_userkit` |
| `SuppressionList` | `reason`, `suppress`, `release` | `_userkit` |
| `SessionStore` | `load`, `save`, `delete` | `session_store` |
| `QuotaStore` | `configure`, `spend`, `used` | `quota` |
| `SecondFactorStore` | `credentials`, `add`, `remove`, `touch` | `_secondfactor` |
| `ChallengeStore` | `put`, `peek`, `consume`, `discard` | `_secondfactor` |
| `ReplayLedger` | `claim` | `saml` |
| `ObjectStore` | `read`, `write`, `stat`, `exists`, `list`, `delete`, `url`, `path` | `objects` |
| `UploadStore` | `create`, `read`, `advance`, `delete`, `expired` | `objects` |
| `IdempotencyStore` | `reserve`, `store`, `release` | `middleware.idempotency` |
| `RateLimitStore` | `configure`, `acquire` | `middleware.ratelimit` |
| `FlagProvider` | `enabled` | `flags` |
| `Preferences` | `allows` | `notifications` |
| `Channel` | `name`, `deliver` | `notifications` |
| `MessageSender` | `send` | `notifications` |
| `PushSubscriptions` | `for_recipient`, `remove` | `notifications` |
| `Service` | `start`, `drain` | `services` |

Two things this table makes visible that no single file could:

* **Five verbs mean "take this key, atomically, once":** `ChallengeStore.consume`,
  `IdempotencyStore.reserve`, `ReplayLedger.claim`, `RateLimitStore.acquire` and
  `QuotaStore.spend`. These really are different operations — `spend` answers
  with seconds-to-wait so a refusal can carry a truthful `Retry-After`, `claim`
  answers with a boolean — but nothing at a call site says which distinctions
  are real. `wreath.quota.QuotaStore` is the pattern to copy: its docstring
  opens by naming its sibling and explaining the one way it differs.
* **Four verbs mean "remove":** `delete` (session, object, upload), `release`
  (suppression, idempotency), `discard` (challenge) and `remove` (second factor,
  push subscriptions).

The names are not being changed. They are public API, the win would be cosmetic,
and a rename costs every application that implements one. The index is the fix.

### The shared primitive

`wreath.store` is where the PostgreSQL half of these lives: `Keyed`, `Column`,
`PostgresStore`, `MemoryStore`, `Sql`, `rows_affected`. Eight modules build on
it — sessions, quota, second factor, rate limits, SAML replay, log, entity,
idempotency.

Four table-owning stores do **not**: `webhooks.PostgresWebhookInbox` and
`PostgresWebhookOutbox`, `workflows.PostgresWorkflowStore`, and
`objects.MemoryObjectStore`/`MemoryUploadStore`. The webhook pair is the
interesting case and the reason the boundary is written down here: they are
handed a *session* per call and never hold a `Database`, which is exactly what
`PostgresStore` assumes it has. That is a real difference, not an oversight —
and it is why `Wreath._components_by_database` has a third case for an owner
that cannot say which database its tables belong in.

## Reaching the accelerator

Two idioms, and they are **not** interchangeable. Pick by whether the twin has
to stay reachable in a running process.

**Module-scope selection**, the common case (about two dozen sites — `response`,
`queue`, `kv`, `cache`, `templates`, `protobuf`, `negotiation`, `binding`,
`_b64`, `orm.compiler`, and the middleware):

```python no-check="a fragment: `...` stands in for the twin's real signature"
if _core is not None and hasattr(_core, "sse_frame"):
    _sse_frame = _core.sse_frame
else:  # pragma: no cover - exercised by the WREATH_PURE parity run
    def _sse_frame(...): ...
```

The arm is chosen once. Callers get the native callable with no extra Python
frame, which is why `binding` and `rooms` use it on the response path. The twin
is unreachable in a given process, so it needs the `WREATH_PURE=1` sweep and the
`pragma` that says so.

**Per-call selection**, where both branches must stay live in one process
(`_auth.jwt`, `_auth.cedar_engine`, `_webpush`):

```python no-check="a fragment: the dispatch, without the function it sits in"
_native_parse = getattr(_core, "jose_parse", None) if _core is not None else None
...
if _native_parse is not None:
    ...
```

This costs a branch per call and buys testability without a subprocess.

The consequence for mutation testing is in `AGENTS.md`: **a mutant killed in one
execution mode and surviving in another has survived.** A module-scope site's
pure arm is only reached under `WREATH_PURE=1`, so scoring it from the native
run alone overstates it — which is exactly what happened to `_auth/jwt.py`,
by four mutants.

Prefer naming both arms in tests rather than monkeypatching the selection. A
test that nulls the accelerator and then asserts is partly asserting that its own
patch ran; `tests/test_b64.py` parametrizes over the two functions by name
instead, and skips the native parameter when there is no `_core`.

## Retry and backoff

One implementation — `wreath._jobcore.compute_backoff` — and three sets of
parameter names on top of it, because each subsystem named its knobs before the
arithmetic was shared:

| Concept | `wreath.jobs` | `wreath.http_client` | `wreath.webhooks` |
| --- | --- | --- | --- |
| first delay | `backoff_base` | `backoff_base` | `retry_delay` |
| ceiling | `backoff_cap` | `backoff_cap` | `retry_cap` |
| multiplier | `backoff_factor` | fixed at 2 | fixed at 2 |
| jitter | `backoff_jitter` | none | fixed at ±20% |
| curve | `backoff` (`"exp"`, …) | fixed | fixed |

These are public keyword arguments on all three, so they are not being renamed.
Read the table as the translation: `webhooks`' `retry_delay` is `jobs`'
`backoff_base`. If you are adding a fourth subsystem that retries, use the
`jobs` spelling — it is the complete one, and it is the one `compute_backoff`
itself is parameterized in.
