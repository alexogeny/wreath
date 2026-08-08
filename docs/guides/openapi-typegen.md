# OpenAPI and typed clients

Because your routes already declare their types, Wreath can describe your API to
the rest of the world without you writing that description by hand — and keep it
honest, because it is generated from the same signatures your handlers run on.

## User story: fail CI when the frontend client drifts

> *As an API author, my frontend uses a generated TypeScript client. When I change
> a route's response type, I want CI to fail unless the committed client was
> regenerated — so the frontend can never quietly fall out of sync with the API.*

```bash
# regenerate after changing a handler, and commit the result
wreath typegen app:app --output ./client --react-query

# in CI: exit non-zero if the checked-in client is stale
wreath typegen app:app --output ./client --react-query --check
```

`--check` regenerates into a scratch area and compares against what's committed;
a drift is a non-zero exit — the same ergonomics as `wreath migrations check`.
Because the client and the running API both derive from your typed handler
signatures, a green check *is* the guarantee that they agree.

## OpenAPI

`wreath.openapi` produces an OpenAPI 3.1 document from your typed routes, and a
page to serve it:

```python
from wreath.openapi import generate_openapi
spec = generate_openapi(app)
```

The generator refuses an unsupported annotation instead of publishing an empty
schema. `Field` aliases, descriptions, examples, numeric/length bounds and
patterns come from the same metadata runtime validation uses. Route metadata
owns the rest of the operation contract:

```python
from wreath.openapi import ResponseSpec

app.add_security_scheme(
    "bearerAuth",
    {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
)

@app.post(
    "/widgets",
    status_code=201,
    response_description="Created widget",
    responses={409: ResponseSpec(Conflict, description="Name conflict")},
    security={"bearerAuth": ()},
    deprecated=False,
)
async def create(request, body: NewWidget) -> Widget:
    ...
```

`include_in_schema=False` withholds an internal operation. The declared success
status is also the runtime status for a plain return value, so documentation and
dispatch cannot disagree. `compare_openapi(previous, current)` returns stable
breaking-change records for removed operations/responses and newly required or
tightened parameters; use it as the compatibility decision in CI.

## Typed clients

`wreath.typegen` goes a step further and renders a client for your frontend —
TypeScript types, a `fetch` client, and React-Query hooks — from that same
canonical model. Your frontend's understanding of your API can't drift from the
API itself, because both come from one source:

```bash
wreath typegen app:app --output ./client --react-query
```

Two things beyond the routes come along, when your application has them. The
authorization vocabulary becomes typed permission flags, so asking about an
action your API does not enforce is a compile error rather than a permanent
`false`. And each [calculated view](calculated-views.md) your routes use becomes
a `series.ts` type whose fields are the measure names you declared — a component
destructures `line.values` with the compiler already knowing whether it can
contain `null`. Neither module ships for an application that declares neither.

## A client that behaves, because the server said how

First-class policy and custom hooks [describe themselves](middleware.md#what-policy-and-custom-hooks-tell-the-document),
so the document carries more than shapes. An operation guarded by
`IdempotencyMiddleware` is documented as reading an `Idempotency-Key`; one
behind a rate limiter is documented as answering `429` with a `Retry-After`.

Alongside the header and response documentation, each operation carries the
behaviours a client may act on, under `x-wreath-behaviours`. The vocabulary is
closed — `idempotency-key`, `retry-after`, `etag`, `csrf-token` — and a name
outside it is refused when the document is generated, so a typo cannot reach a
consumer. A foreign generator ignores an unknown `x-` key, which is the right
failure mode: it sees a document it fully understands, minus an optimisation.

`wreath typegen` turns those into a `behaviours.ts` module:

```ts
import { send } from "./behaviours";

// Sends an Idempotency-Key because the server declared it honours one,
// and waits out a Retry-After instead of hammering.
const response = await send("createSighting", "/sightings", {
  method: "POST",
  body: JSON.stringify(sighting),
});
```

The module ships only for an application that declares something, it imports
nothing, and its retry ceiling is a visible constant rather than a loop — a
client that retries without a bound turns one struggling origin into an outage.

Removing a behaviour is a **breaking** change, and `compare_openapi` reports it
as one. A consumer that silently stops sending an idempotency key still
compiles and still passes its tests; it duplicates a write the first time it
retries, which is exactly the kind of regression a compatibility gate exists to
catch.

## Calling a sibling service, typed

`--target python` emits a client for another wreath service instead of a
frontend one:

```bash
wreath typegen llama_service:app --target python --output ./llama_api \
  --class-name LlamaClient
```

You get `models.py` (a dataclass per shape) and `client.py` (a `ServiceClient`
subclass with one typed method per operation). Calling it is ordinary Python:

```python
from llama_api import LlamaClient

llamas = LlamaClient(app.http_client("llamas"), token=service_token)
llama = await llamas.get_llamas_by_llama_id(7)   # -> Llama, not Any
```

The generated client **subclasses `ServiceClient` and contains no transport**.
Pooling, retries, rate limiting, origin pinning and token refresh stay in
`wreath.http_client`, so a fix there reaches every generated client without
regenerating one of them.

Responses bind through `wreath.binding.validate` — the same function the
provider runs on the way in — so an extra field on the wire is refused here
exactly as it would be there. That is deliberate: a provider that starts
sending a shape you do not model is something to hear about at the boundary,
not three layers in.

Where the provider declared an [idempotency
behaviour](middleware.md#what-policy-and-custom-hooks-tell-the-document), the generated
method takes an `idempotency_key` and defaults it, because the server said it
honours one.

### Pinning the contract

Each generated client carries a `SPEC_DIGEST` over the document it was built
from, and a `spec.json` beside it holding that document. It is **not** checked
at runtime — a client that refused to start because the provider added an
optional field would be an outage generator, and OpenAPI calls that change
compatible. The pin is for CI:

```bash
wreath typegen llama_service:app --target python --output ./llama_api \
  --check-contract
```

That compares the provider *now* against the document the client was generated
from, and exits non-zero only on a **breaking** change — a removed operation, a
new required parameter, an optional one made required, a removed response
status, a removed behaviour. Additions are compatible and say nothing.

The digest alone could not do this. A digest answers *changed or not*; telling
a breaking change from a compatible one needs the previous document itself,
which is why `spec.json` is emitted and kept.

Two checks, two questions, and it is worth keeping them apart:

| Flag | Asks | Fails when |
| --- | --- | --- |
| `--check` | Did you forget to regenerate? | The files on disk differ from what the generator would emit now |
| `--check-contract` | Has the provider broken you? | Regenerating would change your call sites |

`--check-contract` refuses rather than passing when there is no pinned document
to compare against: a gate that succeeds because it found no baseline is a gate
with nothing to check, and it would read as a green build forever.

## A schema for other languages

`--target proto` emits a `.proto` of the shapes this API exchanges, from the
same model that produced the OpenAPI document and the two clients:

```bash
wreath typegen llama_service:app --target proto --output ./schema
```

The usual arrangement is a hand-maintained `.proto` beside a hand-maintained
REST contract, agreeing by discipline. These cannot disagree, because one model
produces both.

Two things to know before you depend on it. **Field numbers come from
declaration order**, so reordering a field in the source dataclass renumbers it
on the wire — a breaking change no test on either side will notice. Pin the
file in review, or declare the message with
[`wreath.protobuf`](protobuf.md), where numbers are explicit. And **no
`service` block is generated**: turning REST operations into RPCs means
inventing a request and response message per operation, and that invented
surface would be a second contract with nothing to check it against.
`wreath.grpc` declares its methods against real `@message` classes, which is
where an RPC contract belongs.

What proto3 cannot express is **refused by name** — a heterogeneous tuple, a
multi-type union, an unannotated value, a nested `repeated`. A schema that
compiles and describes the wrong bytes fails in another team's decoder, which
is the worst place to find out.

**Reference:** [`wreath.openapi`](../reference/openapi.md),
[`wreath.typegen`](../reference/typegen.md).
