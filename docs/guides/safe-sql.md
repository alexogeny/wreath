# Safe SQL

Most of the SQL a Wreath application runs is compiled, and compiled SQL cannot
be injected into: `Shipment.select().where(Shipment.reference == needle)` puts
`needle` in a bind parameter because there is no other place for it to go. That
covers the ordinary day, and if it covered every day this page would not exist.

Some statements have to be written out. A lateral join, a window function, a
recursive CTE, a query whose plan you have already read and do not want a
compiler's opinion about — `Session.raw` exists for those, and it means what it
says: wreath does not parse, rewrite, or cache the SQL you give it.

And that is where the oldest defect in web software gets back in.

## The two spellings

```python
sql = f"SELECT id FROM shipments WHERE reference ILIKE '%{needle}%'"   # a hole
sql = "SELECT id FROM shipments WHERE reference ILIKE $1"              # not
```

Nothing in Python objects to the first one. The type checker sees a `str`. The
test suite sees a working search endpoint. The review sees a line that reads
better than the second one — because an f-string is *supposed* to read better,
that is what it is for. So the defect survives, and it keeps surviving, and the
industry's answer for twenty years has been to ask people to notice.

Python 3.14 made the difference visible to the machine instead. A **t-string**
(PEP 750) is not a string. It evaluates to a `Template` that holds the literal
parts and the interpolated values separately, exactly as the compiler saw them:

```python
from wreath.sql import Identifier

rows = await session.raw(
    t"SELECT id, reference FROM {Identifier(schema, 'shipments')} "
    t"WHERE org_id = {org} AND reference ILIKE {pattern}"
).fetch()
```

`org` and `pattern` come out as `$1` and `$2`, bound. There is no value the
caller can supply — no quote, no comment marker, no `UNION`, no stacked
statement — that changes what this query *is*, because their text is never
concatenated into it. The statement's shape was fixed when the module was
compiled.

Note what the fix costs at the call site: one character, at the quote.

## The two things a parameter cannot be

PostgreSQL resolves some things while it is still parsing, long before a bind
value exists, so those genuinely have to go in the text. There are exactly two,
and each has a type that says so.

**A relation or column name** is an `Identifier`. It is double-quoted, any
embedded quote is doubled, and a NUL is refused:

```python
t"SELECT * FROM {Identifier('northwind', 'shipments')}"   # "northwind"."shipments"
```

Quoting makes a hostile name *harmless* rather than rejected — it becomes a
table that does not exist, and the statement fails with an ordinary "relation
does not exist". Deciding which names a caller may reach is a different
question, and `Identifier` deliberately does not answer it: look the name up in
a directory the application owns.

**Syntax** is a `Fragment`, and it is the one escape hatch. `ASC`, `NULLS LAST`,
a whole `ORDER BY` assembled from an allow-list — no amount of binding can
express those. A `Fragment` is spliced in unquoted, so its text must come from
the application rather than from the request:

```python
DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
order = Fragment(DIRECTIONS[requested])       # KeyError, not an injection
```

## Clauses compose

A t-string interpolated into another t-string is spliced rather than bound, and
its parameters are renumbered into the outer statement. So an optional filter is
an ordinary Python expression:

```python
status_clause = t"AND status = {status}" if status else t""
rows = await session.raw(
    t"SELECT id FROM shipments WHERE org_id = {org} {status_clause}"
).fetch()
```

This is the part that decides whether the safe spelling actually gets used.
Dynamic `WHERE` clauses are why people reach for string building in the first
place, and a safe API that cannot express them is a safe API that loses.

## When you do not need any of this

A plain `str` is still perfectly good, and still means what it always meant:

```python
await session.raw("SELECT id FROM shipments WHERE id > $1 LIMIT 20", after).fetch()
```

A statement written out in full, with its values in `$1`-style placeholders, has
nothing interpolated into it and nothing to get wrong. The t-string is for the
case where the statement is *assembled* — which is exactly the case where the
f-string used to be.

## What notices when you get it wrong

The [hardening](hardening.md) ruleset reports `sql-interpolation` for SQL built
by an f-string, `%`, `+` or `.format()` from a value the caller supplied — at
boot under `warn`, and as a refusal to boot under `block`. Its suggested remedy
is this page.

Reference: [`wreath.sql`](../reference/sql.md),
[`wreath.orm`](../reference/orm.md).
