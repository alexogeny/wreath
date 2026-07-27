# 0010. Wreath's own servers accept a one-shot response message

Date: 2026-07-27
Status: Accepted
Absorbs: the reasoning of the retired `neo.response one-shot ASGI extension`
record, which this supersedes and renames.

## Context

A standard ASGI response costs two awaited `send` calls carrying two dict
messages: `http.response.start`, then `http.response.body`. For a complete,
non-streaming response — the overwhelming majority — both are known at once, and
the second call exists only to satisfy message framing.

On Wreath's own servers, where sends resolve without suspending, that message
pair is measurable per-request overhead rather than a scheduling boundary.

## Decision

Wreath's servers, native and pure twin, advertise `"wreath.response"` in
`scope["extensions"]` and accept a single message:

```python
{"type": "wreath.response", "status": int, "headers": list, "body": bytes}
```

which starts, frames, and completes the response in one send. Validation and
wire output are byte-identical to the equivalent start+body pair.

The framework uses it only when the scope advertises it *and* the response
object does not override `Response.__call__` (`src/wreath/app.py:1655`). Every
other server gets the standard two-message sequence.

## Consequences

- Wreath on Wreath saves one awaited send per response; Wreath on uvicorn or
  hypercorn behaves exactly as before, which keeps `AGENTS.md`'s separability
  rule intact.
- A user's custom `Response` subclass that overrides `__call__` opts out
  automatically — the framework cannot know its send sequence is equivalent.
- The extension is a Wreath-specific ASGI extension, so it is documented as
  Wreath-native behaviour rather than portable behaviour.
- Two send paths exist and both must be tested, since the fast path is exactly
  the one a portable-server test never exercises.

## Alternatives rejected

- **Always send one message.** Rejected: it breaks every conforming server and
  violates the separability rule.
- **Detect a fast server by name.** Rejected: capability negotiation belongs in
  `scope["extensions"]`, which ASGI provides for this.
- **Skip the extension and optimise the two-message path instead.** Partly done
  anyway, but the second `await` remains; the extension removes it rather than
  making it cheaper.

## What would reverse this

An ASGI revision that frames a complete response in one message. The extension
would then be the standard spelling, and this record becomes an implementation
note.
