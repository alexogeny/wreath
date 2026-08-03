# `wreath.quota`

A rate limit says how *fast*; a quota says how *much*, this month. Reach for
this when an allowance has to hold across a billing period and reconcile with an
invoice — and reach for `wreath.middleware.ratelimit` when the question is
bursts per second. The two are decided in one hook, so a caller never receives a
429 that contradicts a 402.

The module ships two halves that are deliberately different in kind: a counter
that **refuses a cost** at the boundary, and a set of declared states that
**establish a fact** for the policy set to interpret. Nothing here rates,
invoices, or charges.

Reference: [the quotas guide](../guides/quotas.md).

::: wreath.quota
