# 0020. A test double is never more capable than what it doubles

Date: 2026-07-27
Status: Accepted

## Context

`tests/orm/conftest.py::FakeConnection` was scripted with Python `str` and `int`
rows. Thirteen introspection tests drove it, all green, for a long time.

The real driver has **no codec** for `name`, `oid`, `"char"`, `int2[]` or
`int2vector` — the exact types a `pg_catalog` read projects — and returns raw
wire bytes. `_validate_model` then did `str(row[2])`, so a column name became
the literal `"b'id'"` and **every column of every table reported
`missing_column`**.

The logic those tests covered was correct the whole time. The read beneath it
had never been exercised, because the fake modelled a driver that does not
exist. `validate_schema="error"` is the default, so no Wreath application with
an ORM had ever completed lifespan startup against a real PostgreSQL.

The same shape appeared twice more: `_TransactionDouble`'s query methods made no
refusal checks, so a statement written inside `async with
connection.transaction()` was accepted by the double and rejected by the driver
— and `_passes/driver.py` writes every one of its statements there.

## Decision

A double must not succeed where the real thing would fail. Where a real
implementation refuses — an unencodable parameter, an unprepared statement, a
type with no codec — the double refuses identically.

Prefer doubles that **derive** their refusals from the real implementation
rather than restating them, so the two cannot drift.

## Consequences

- Fixtures are more work to write and reject inputs that "obviously" should have
  worked. That rejection is the value.
- Some properties cannot be doubled honestly at all, and those tests are gated
  on a real PostgreSQL (`WREATH_TEST_POSTGRES_DSN`) rather than faked. Parameter
  type inference, query plans, lock behaviour and DST boundaries are that class.
- Skipping must be loud: `tests/conftest.py` prints a banner naming the count
  whenever the gated suites skip, because they went a long time without running
  once and nobody noticed.
- Two known gaps remain and are tracked rather than tolerated silently: scripted
  results are not `Record`-shaped, and a `Statement` on a double is never
  *prepared*, so SQL naming a nonexistent table succeeds there and fails against
  a server.

## Alternatives rejected

- **A more capable fake, for convenience.** Rejected: this record exists because
  that is what shipped. A double more capable than the driver hides exactly the
  defects it exists to catch.
- **Integration tests only.** Rejected: they are slower and need infrastructure,
  and most logic genuinely is testable in isolation. The rule is about fidelity,
  not about abandoning doubles.
- **Assert on the double's shape in its own tests.** Rejected as insufficient —
  it pins the double to itself, not to the driver.

## What would reverse this

Nothing. The refinement worth making is mechanical derivation: doubles that read
the driver's codec table rather than restating it, which would make the drift
impossible rather than merely forbidden.
