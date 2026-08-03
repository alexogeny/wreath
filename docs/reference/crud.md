# `wreath.crud`

Generate REST routes from an ORM model, off unless you ask for them twice: once
at the application (`Wreath.enable_crud()`) and once per model (`crud_router()`
or `Wreath.crud()`). A model is never exposed because it happens to exist.

Columns that look like secrets — `password`, `*_hash`, `token`, `secret`,
`api_key` — are withheld from responses and from accepted input until you name
them in `expose=(...)`. Read that as a backstop rather than a boundary: it
withholds the column nobody thought about and cannot recognise a secret that
does not look like one, so `fields=(...)` is the control. Retrieval columns
(`Vector`, `TsVector`) are withheld on firmer ground — they are how a row is
found, not what it says. The guide is [Generated CRUD routes](../guides/crud.md).

::: wreath.crud
