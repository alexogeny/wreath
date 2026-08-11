# What `wreath port` cannot translate, and why

Every row here is a site the porter recognises, names, and refuses. They are
not analysis failures — the analyzer knows exactly what it is looking at. They
are places where **wreath has no construct that means the same thing**, so any
translation would be a guess dressed as an answer.

Measured over the seven-application corpus. Rerun `~/scratch/port-sweep.sh` and
the counts move; the reasons should not.

## Blocked on a wreath feature — 15 sites

| n | rule | what the source does | what wreath would need |
| ---: | --- | --- | --- |
| 10 | `mig.schema_op` | A migration adds a check or exclusion constraint, or a constraint whose kind the call does not name | Model-level constraint declaration beyond `check=`, so the constraint has somewhere to live before a migration can carry it |
| 3 | `exc.http_unmapped` | Raises a status wreath ships no exception class for | Either the class, or the documented recipe (subclass `HTTPException`, set `status`) promoted into something the emitter can write |
| 1 | `mig.unmodelled_type` | A column typed `Time`, `Interval`, `Enum`, `INET`, `TSVECTOR` or fixed-width `CHAR` | The `PgType`. Until one exists the model cannot describe the column, so the migration cannot be derived from it |
| 1 | `mig.data` | `op.get_bind()` — the revision rewrites rows, not just schema | A data-migration story. Wreath generates schema from the catalog and has no place to put row edits |

These are wreath roadmap items, not porter bugs. Adding any one of them makes
the corresponding sites translatable without touching the analyzer.

## Correctly asking a human — not a gap

Distinct from the above: the porter *could* emit something, and shouldn't.

- **`route.status_code_empty_body`** — the route declares 204 or 304 and the
  handler returns a body. The source is already wrong; both repairs change
  behaviour, so the choice is the author's.
- **`webhook.hmac`** — the hand-rolled check compares only the digest, so a
  captured request replays forever. Translating it faithfully would carry the
  vulnerability across. `HMACWebhookVerifier` is the target, and adopting it is
  a security fix, not a rename.
- **`orm.query.first` without `order_by`** — "the first row" is whatever
  postgres returned that day. There is no correct emission for an unspecified
  order.
- **`lifespan.ctx`** where the body does not split at the `yield`.

## Not a gap, just unwritten

Sites whose target is exact and whose rewrite nobody has implemented yet. These
should not be confused with the table above:

`orm.query.get_or_create` (10) — static field values expand onto `fetch_one`
plus `create`; only dynamic defaults resist.
`auth.oauth` (1) — `oauth2_login()` / `ClientCredentials`, drop authlib.

Foreign frameworks are no longer all-or-nothing. The application object, its
routers, the route decorators with their path converters, HTTP errors and
redirects are translated for Flask, Bottle, aiohttp, Pyramid, Tornado and
Django. What is still unwritten there is the *binding* half — `request.args.get`,
`match_info`, `self.get_argument`, `request.matchdict` and the form/header/cookie
reads all become parameters, and that rewrite has to delete a statement from the
body and add a parameter to the signature at the same time. Also unwritten:
Tornado's handler class into one function per verb, Pyramid's `add_route` name
joined to its `view_config`, and Django's `path()` URLconf joined to its views —
each of those is a cross-module move rather than a local edit.

`bg.celery` and `route.response_class` were both here, and both are now written.
What is left under each name is the half that genuinely resists, and each is a
row in the table above rather than an unwritten rewrite:

- `bg.celery` — a `queue=` (wreath's queue *is* the runner, so a second queue is
  a second `app.jobs(...)`), a `self.retry()` (wreath retries by letting the
  handler raise, so there is no call to rename and deleting one changes what
  happens on failure), a `countdown=` (`enqueue` takes `run_at=`, an absolute
  instant), a `.delay()` in a plain `def` (`enqueue` is a coroutine, and making
  the caller `async` changes every one of *its* callers), and the database name
  on `app.jobs(...)`, which nothing in the Celery call says.
- `route.response_class` — a class wreath would not have picked for what the
  handler returns. `JSONResponse` over a dict is the keyword describing what
  happens anyway; `JSONResponse` over a `str` sends `"ok"` with the quotes as
  JSON where wreath sends `ok` as `text/plain`, and `HTMLResponse` changes the
  content type of every response the route sends.

Single-field validators and query-parameter constraints belong here too rather
than in the human column: `narrow()` on the column, `@rule()` across fields,
and `Pattern`/`Length` all exist and are declarative.
