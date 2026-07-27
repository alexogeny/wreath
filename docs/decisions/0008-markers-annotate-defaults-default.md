# 0008. Markers live in `Annotated`; the request is the first parameter

Date: 2026-07-27
Status: Accepted

## Context

FastAPI spells a bound parameter `limit: int = Query(20)` — the marker occupies
the default slot, so the marker and the default compete for one position. That
has two consequences a framework inherits: a parameter with a marker cannot have
an ordinary Python default without wrapping it, and the signature no longer
means what Python says it means.

Separately, handlers need the request. Passing it by name, by type annotation,
or by dependency all work, and all three make "does this handler get the
request?" a question you answer by reading the body.

## Decision

Markers live in `Annotated` metadata; the default slot holds a plain Python
default. The request is always the first handler parameter.

```python
async def list_items(request: Request, limit: Annotated[int, Query(minimum=1)] = 20): ...
```

A marker used in the default slot is **refused at route compilation** with a
`TypeError` naming the parameter and the correct form
(`src/wreath/binding.py`, `inspect_handler`). Because compilation runs during
the ASGI lifespan scope, that is a startup failure under any server — not a
per-request surprise.

`Depends` is deliberately outside the refused set: a dependency in the default
slot is the correct spelling.

## Consequences

- The signature reads as Python. `= 20` is the default, and it is the default.
- A FastAPI-shaped handler fails loudly at startup rather than binding nothing
  and silently passing the marker object through as the value, which is what it
  did before the refusal existed (ADR 0019).
- `docs/from-fastapi/` must teach the translation, and four documentation pages
  showed the wrong form until the executable-docs gate caught them (ADR 0021).
- A dependency's *own* scalar parameters are not bound from the request, so a
  binding marker on one is honoured only in a handler signature. That asymmetry
  is currently a defect rather than a decision, and is tracked.

## Alternatives rejected

- **Marker in the default slot, FastAPI-style.** Rejected: it spends the default
  slot on metadata and makes an ordinary default unexpressible.
- **Accept both spellings.** Rejected as strictly worse than either — two ways to
  write one thing, one of which silently loses constraints.
- **Request by annotation anywhere in the signature.** Rejected: positional makes
  the answer visible without reading the body, which matters most to the reader
  who did not write the handler.

## What would reverse this

A typing feature that lets a marker and a default share the default slot without
ambiguity. `Annotated` exists precisely because they cannot.
