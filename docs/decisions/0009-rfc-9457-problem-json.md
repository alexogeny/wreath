# 0009. Every built-in error is an RFC 9457 problem document

Date: 2026-07-27
Status: Accepted

## Context

The de facto Python API error shape is `{"detail": "..."}` — a convention with
no specification, no type field, and no room for structured detail. A client
consuming several such APIs cannot tell one error taxonomy from another, and a
client consuming one cannot distinguish "this failed because of field `email`"
from "this failed" without parsing prose.

RFC 9457 (formerly 7807) specifies `application/problem+json` with `type`,
`title`, `status`, `detail`, and `instance`, and permits extension members.

## Decision

Every built-in HTTP error is an RFC 9457 problem document — never
`{"detail": ...}` (`src/wreath/exceptions.py:5`). Validation failures carry
their field errors as an `errors` extension member, each with `loc` and `type`.

## Consequences

- The error contract is machine-readable and specified by a document that is not
  ours.
- Field-level validation errors survive to the client as structure rather than
  as a rendered string. This is why `validation_error_response` was removed
  rather than kept: it produced an `UnprocessableEntity` whose detail was
  `"N validation error(s)"` and dropped the list, and an `HTTPException` has
  nowhere to carry structured field errors.
- Clients written against `{"detail": ...}` must change. `wreath port` reports
  this rather than translating it, because the shape change is semantic.
- `set_validation_formatter()` exists so an application can shape the document
  without abandoning the media type.

## Alternatives rejected

- **`{"detail": ...}` for familiarity.** Rejected: the familiarity is worth less
  than the contract, and the migration cost is paid once.
- **Problem+json only for 5xx.** Rejected: a client would need two parsers, and
  the errors that most need structure are 4xx.
- **A Wreath-specific error schema.** Rejected: inventing a shape when a
  published one fits is how ecosystems fragment.

## What would reverse this

Nothing foreseeable. RFC 9457 would have to be superseded, and the successor
would then be the decision.
