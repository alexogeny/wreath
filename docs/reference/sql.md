# `wreath.sql`

SQL that cannot carry an injection, because the values never enter the text.

A t-string keeps the literal parts of a statement and the interpolated values
apart, exactly as the compiler saw them, so `Statement` can turn one into
`$1`-style SQL mechanically — and what was interpolated can only ever leave as a
bind parameter. `Identifier` and `Fragment` cover the two things a parameter
cannot be: a relation name, and syntax.

Reach for it any time part of a statement is a value. The guide is
[Safe SQL](../guides/safe-sql.md), and `wreath.orm.Session.raw` is where a
statement usually goes.

::: wreath.sql
