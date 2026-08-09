---
description: wreath new writes a project that already runs, tests green, and has the wiring right.
keywords: wreath new, scaffold, start a project, project template, new app, boilerplate, starter
---

# Starting a project

[Your first app](index.md) builds one up by hand, a line at a time, which is the
right way to learn what the pieces are. Once you know, this writes the same
thing out for you:

```bash
wreath new shop
cd shop
cp .env.example .env
pytest                       # already green
wreath dev shop.app:app
```

```
shop/
  pyproject.toml
  .env.example               # committed; `.env` is not
  .gitignore
  README.md
  shop/
    __init__.py
    app.py                   # build() and a module-level app
    config.py                # the environment, bound once at startup
    routers/
      items.py               # one resource, four concepts
  tests/
    test_items.py            # four tests, passing
```

Two more flags:

```bash
wreath new shop --database postgres    # an ORM model, and the migration loop in the README
wreath new shop --frontend react       # a React app wired to `wreath typegen`
wreath new shop --database postgres --tenancy   # ... isolated by PostgreSQL role
wreath new shop --forge codeberg       # ... and CI for the host it will live on
```

**It refuses a directory that has anything in it**, and there is no `--force`.
Overwriting your work to save you a `mkdir` is not a trade worth making.

## What it gets right that a blank file does not

None of this is hard. All of it is written down somewhere, and none of it is
written down in the same place, which is why the first half hour of a project
goes on it:

- **`.env` has no comment syntax.** wreath's dotenv dialect is `KEY=value` and
  nothing else, so a `#` line raises a `ValueError` naming the line number
  rather than being skipped. An annotated template produces a `.env` that fails
  to load on its first line. An empty value is not "use the default" either — it
  binds as the empty string, or fails to coerce. The generated `.env.example`
  carries working values and no prose; what each key *means* lives beside the
  dataclass that reads it.
- **`Depends` goes in the default, never inside `Annotated`.** Every other
  marker — `Query`, `Body`, `Path`, `Header` — goes inside `Annotated` so the
  default stays an ordinary Python default. `Depends` is the exception, and the
  other spelling is refused at startup rather than quietly binding the parameter
  from the request body.
- **A dependency is called with the request**, even one that ignores it — so a
  shared store is `Depends(open_catalogue, scope="app")` over a function, not
  `Depends(Catalogue)` over the class.
- **The return annotation is the response contract.** The generated handlers
  return declared dataclasses, not `dict`, because that annotation is what
  wreath validates against, what the OpenAPI document describes, and what
  `wreath typegen` turns into a TypeScript interface. A handler annotated
  `-> dict` generates a client typed `Record<string, unknown>`, which compiles
  and tells the front end nothing.
- **`build()` as well as `app`.** A factory is what a test wants — one
  application per test rather than one shared by all of them.
- **`pythonpath = ["."]` in the generated `pyproject.toml`**, because `tests/`
  carries no `__init__.py` and without it `import shop` fails from inside a test.

The scaffold's own tests import the generated project, drive requests through
it, run its suite, and type-check the generated React app against the generated
client. A template that has drifted from the framework fails there, rather than
in your first hour.

## With a front end

```bash
wreath new shop --frontend react
cd shop
wreath typegen shop.app:app --output web/src/api --react-query
cd web && npm install && npm run dev
```

`web/src/api/` is **gitignored deliberately**. It is a build product of the
route table, and a committed copy is a client that lies about the API the moment
somebody edits a handler. Regenerate it whenever a route changes, and run the
same command with `--check` in CI so a change nobody regenerated fails the build.

The generated `App.tsx` is hand-written; everything it calls is not. The hooks
are named by each route's `operation_id`, so renaming a *path* does not rename
every call site, and `item.price` is a field of a generated `Item` — renaming it
on the server is a TypeScript error at build time rather than `undefined` in a
browser.

## With a database

```bash
wreath new shop --database postgres
```

This adds `shop/models.py` and registers the database in `build()`. There is no
`create_all`: the application validates its schema at startup and refuses a
database that does not match, and [`wreath migrations`](../guides/migrations.md)
is what changes one. The README carries the loop, including the two steps that
surprise people — creating the schema is a separate statement from applying an
artifact, because an artifact describes tables and not which schema they land
in, and `apply` reads `WREATH_MIGRATION_DSN` rather than reusing your request
credentials.

`shop/config.py` has **no default** for the DSN. Guessing at `localhost` and
connecting to the wrong database is worse than refusing to start, so importing
the application without one fails immediately, naming the variable.

## With tenants

```bash
wreath new shop --database postgres --tenancy
```

Adds `shop/tenants.py` — the directory, the resolving `Tenancy`, and the
`TenantSession` alias every tenant-scoped route binds — and installs
`TenancyMiddleware` in `build()`. The registry becomes
`SchemaMode.isolated(isolation="role")`, which is what makes tenant-local SQL
resolve through the search path while the boundary is a role and a grant set.

It needs `--database postgres` and refuses without it: tenant isolation *is* a
schema and a role per tenant, so there is nothing to isolate without one.

The generated `TenantSession` is the point of the option. A tenant-isolated
registry bound with a bare `FromORM` is refused at route-compile time, so the
declarative spelling is the safe one and there is no way to reach the data
without a tenant. See [Multi-tenancy](../guides/tenancy.md) for what the
boundary does and does not stop.

## With continuous integration

```bash
wreath new shop --forge github        # ... or gitlab, codeberg, forgejo, gitea
```

Whichever host you name gets the file it actually reads, running the same three
checks:

| forge | file |
| --- | --- |
| `github` | `.github/workflows/ci.yml` |
| `gitlab` | `.gitlab-ci.yml` |
| `codeberg`, `forgejo` | `.forgejo/workflows/ci.yml` |
| `gitea` | `.gitea/workflows/ci.yml` |

The checks are `ruff check .`, `pytest`, and `wreath doctor preflight` — each one
a command the scaffold's own suite already runs against a generated project, so
the pipeline is not asking your CI to be the first thing that tries them.

For a project that already exists, or one mirrored to two hosts:

```bash
wreath ci init --forge github --forge codeberg
```

It reads the package name from `pyproject.toml` rather than the directory name,
because a checkout is routinely called something else and a preflight target
built from that names a module which does not import. Like `wreath new`, it
**refuses to write over a CI file that is already there**, and there is no
`--force`.

The checks are declared once and each forge renders them, which is the only
reason to trust a file the test suite cannot execute: a check added for GitHub
and forgotten on the other three would be invisible, and
`tests/test_ci.py` asserts every renderer carries every command instead. What no
test here can do is *run* a GitLab pipeline, so the first push is the real proof
— which is also why nothing generated reaches for a clever runner feature. The
Forgejo and Gitea files name their checkout action by full URL, because a bare
`uses: actions/checkout@v4` resolves against a different host on each and either
default is changeable by whoever runs the instance.

Two things the generated pipeline deliberately does not do. It runs no
front-end job, even with `--frontend react`: `npm run build` needs the
gitignored `web/src/api/`, so that job is real setup rather than a line, and
adding `wreath typegen --check` to it is left to you. And it starts no
PostgreSQL service with `--database postgres`, because the generated suite runs
`build(database=False)` and needs none.

## Then what

```bash
wreath capabilities celery          # what already ships that answers a word you know
wreath doctor preflight shop.app:app --environ
```

The first is worth running before you add any dependency; see
[what you don't have to install](../capabilities.md). The second is
[the preflight report](../guides/preflight.md) — one pass of everything wreath
can check about a built application, and a named list of what it cannot.

For the shape of a bigger application, read
[the camera-trap example](../example/index.md): one application that uses the
parts together, rather than a gallery of snippets that each use one.
