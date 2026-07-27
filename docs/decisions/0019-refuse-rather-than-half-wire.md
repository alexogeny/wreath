# 0019. Refuse a wrong declaration rather than half-supporting it

Date: 2026-07-27
Status: Accepted

## Context

A framework meets declarations it cannot honour: a marker in a position the
binder does not read, a middleware registered where its output arrives too late,
a query shape the compiler cannot express, an argument with nothing to key.

Each has three possible responses — support it, ignore it, or refuse it.
Ignoring is the cheapest to implement and by far the most expensive to use,
because the failure surfaces far from its cause and usually blames the caller.

The instances that produced this record:

- `limit: int = Query(20)` bound nothing, ignored its constraints, and passed
  the `Query` object through **as the value**.
- `SessionMiddleware` registered with `add_middleware` runs after
  authentication, so `SessionIdentityBackend` read nothing and **every protected
  route answered 401 with a valid session cookie**.
- `Depends(page_params)` returned 500; the `Annotated` spelling returned **400
  "invalid JSON body"**, because the parameter was silently reclassified as a
  request body on a `GET`. A 500 says "we broke"; a 400 says "you broke", and
  the caller has no way to know which is true.
- `S3ObjectStore(url_secret=...)` had nothing to key, since SigV4 signs with the
  AWS credentials.

## Decision

Refuse at the earliest point the error is knowable, with a message naming the
offending element **and the correct form**. Prefer declaration or startup time
over request time: route compilation runs during the ASGI lifespan scope, so a
refusal there is a startup failure under any server rather than a per-request
surprise.

Half-supported is worse than refused. It is also *more* code.

## Consequences

- A misdeclared application does not start. This is intended: the alternative is
  an application that starts and is wrong.
- Error messages carry the fix, not just the fault. "Use
  `Annotated[int, Query(...)] = <default>`" is the message, not "invalid
  parameter".
- Some refusals are conservative and will occasionally reject something
  workable. `not_in` against a nullable subquery column is refused outright
  because SQL's three-valued logic returns *no rows* once one NULL appears — it
  passes every test written against clean data and empties a page in production.
- Refusals must be reachable. A refusal that cannot fire is ADR 0024's pattern,
  and two shipped that way.

## Alternatives rejected

- **Warn and continue.** Rejected: a startup warning is read once and filtered,
  and the behaviour it predicts arrives later, detached.
- **Support the alternative spelling.** Rejected where the spelling cannot be
  made to work — the marker-in-default case cannot bind constraints without
  redefining what a Python default means (ADR 0008).
- **Refuse at request time.** Rejected where the error is knowable earlier.
  `Access.cedar(resource=...)` currently parses its entity reference per request
  and answers 500 where the declaration plainly intends 403; that is a defect
  against this record, not an exception to it.

## What would reverse this

Nothing general. An individual refusal reverses when the thing becomes
supportable — which is what happened to `Column.in_` and subqueries.
