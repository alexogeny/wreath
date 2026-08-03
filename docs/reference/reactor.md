# `wreath.reactor`

The event loop behind the experimental `metal` tier: an `asyncio`-compatible
`SelectorEventLoop` that inline-drives non-suspending request coroutines, and
optionally backs every deadline with the native hashed timing wheel instead of
asyncio's timer heap.

::: wreath.reactor
