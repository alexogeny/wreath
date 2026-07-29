---
keywords: sql query builder, prepared statement, placeholders, named read, database query
---
# `wreath.queries`

Named, compiled reads over one model. Reach for it when your application has
started growing a module of small functions that each build the same query with
slightly different filters: declare those reads once, give them names, and they
compile a single time apiece through the ORM's existing plan cache. Reads only —
writing stays with the [session](orm.md), which owns it.

After a declaration's first execution in a registry, its request path binds
named arguments directly into the cached immutable SQL plan. It does not rebuild
a `Select`, walk the expression tree, or derive and hash the query shape again;
eviction from the registry's bounded plan cache still causes an ordinary safe
recompile on the next call.

::: wreath.queries
