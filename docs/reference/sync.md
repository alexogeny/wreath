# `wreath.sync`

Subscribe to a query and be told when its answer moves. A shape is an ordinary
`Select` built from the principal, so the rows a client receives are decided by
the same code that would have served them over a normal route — and a row that
stops matching produces a tombstone rather than lingering on the client forever.

Reach for it when a client renders a list that other people can change, and
polling for it has started to look expensive. Read
[the guide](../guides/sync.md) first; it explains the bound, which is the one
part of the design a caller has to understand.

::: wreath.sync
