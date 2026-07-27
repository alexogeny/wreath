# `wreath.queries`

Named, compiled reads over one model. Reach for it when your application has
started growing a module of small functions that each build the same query with
slightly different filters: declare those reads once, give them names, and they
compile a single time apiece through the ORM's existing plan cache. Reads only —
writing stays with the [session](orm.md), which owns it.

::: wreath.queries
