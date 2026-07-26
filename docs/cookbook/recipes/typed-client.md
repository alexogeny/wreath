# Generate a typed TypeScript client

Your frontend talks to your API through a generated TypeScript client. Because
your handlers already declare their types, Wreath can render that client — types,
a `fetch` client, and React-Query hooks — from the same signatures the API runs
on, so the two can't quietly drift apart:

```bash
wreath typegen app:app --output ./client --react-query
```

`app:app` is `module:attribute` — the module path to your app and the attribute
that holds it. Commit the generated `./client` directory. Regenerate it whenever
you change a handler's request or response type.

The payoff is in CI. Add `--check` and the command regenerates into a scratch
area and compares against what's committed — a drift is a non-zero exit, exactly
like `wreath migrations check`:

```bash
wreath typegen app:app --output ./client --react-query --check
```

Because the client and the running API both derive from your typed handler
signatures, a green check *is* the guarantee that they agree — the frontend can
never fall out of sync with the API without CI catching it first.

A few flags worth knowing:

- `--react-query` also emits React-Query hooks alongside the `fetch` client; omit
  it for just the types and client.
- `--factory` — when the target is a factory function (`app:build_app`) rather
  than a ready application instance, call it to build the app.
- `--allow-unknown` — proceed past a type the generator can't map instead of
  failing; use it sparingly, since an unmapped type is usually worth fixing.
