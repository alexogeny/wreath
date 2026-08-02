# `wreath.privacy`

Erasure, retention and subject access, derived from the foreign-key graph your
models already declare. `privacy.plan("4711")` walks from the data subject
outwards and returns a typed plan: which tables are erased, which columns are
nulled or redacted, which rows are retained under a written exemption — and,
most usefully, which classified tables the traversal could **not** reach.

Nothing is applied and nothing is contacted while planning. The plan is
something to read first, in the same way `wreath infra infer` emits a plan
rather than a stack, and for a sharper version of the same reason: an erasure
that is subtly wrong cannot be undone and the subject has already been told it
is done.

Start with [the guide](../guides/privacy.md), which walks a small schema
through a plan, the five findings, and the two limits this module states out
loud rather than implying away.

::: wreath.privacy

::: wreath._privacy.model

::: wreath._privacy.registry

::: wreath._privacy.declare

::: wreath._privacy.graph

::: wreath._privacy.planner

::: wreath._privacy.execute

::: wreath._privacy.record

::: wreath._privacy.retention

::: wreath._privacy.render
