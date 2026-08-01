# Find out what a service needs provisioned

You have inherited a wreath service and no infrastructure definition, or you
have one and no confidence that it still matches. Ask the application:

```console
$ wreath infra infer myapp:app
```

It imports the application, reads the objects its own declarations built, and
prints the databases, object stores, outbound origins, listener, and the tables
each subsystem owns. Nothing is contacted — no DSN is resolved and no bucket is
listed — so it is safe to run from a laptop against a production target.

## Check the environment a deployment will actually have

The most valuable half is the settings contract, and it needs two things: which
settings model to check, and what is supposed to supply it.

```console
$ wreath infra infer myapp:app \
    --settings myapp.config:Settings=MYAPP \
    --env deploy/production.env
```

Every field of `Settings` is turned into the environment key
[`Environment.bind`](../../guides/config-state.md) will read, and checked
against the dotenv file. A field with no default that nothing supplies is
reported by name, and **the command exits 1**, so this belongs in CI:

```yaml
- run: wreath infra infer myapp:app --settings myapp.config:Settings=MYAPP --env deploy/production.env
```

A renamed field now fails the pipeline instead of the container. The check runs
the other way too: a key the file sets that no field reads is reported as
`[unread-key]`, which is how a typo'd variable that has been doing nothing for a
year finally surfaces.

Use `--environ` instead of `--env` to check against the process's own
environment — useful inside the container image you are about to ship.

## Feed it to something else

```console
$ wreath infra infer myapp:app --format json | jq '.databases[].budgets'
```

The plan is a tree of frozen dataclasses first and a rendering second, so
`--format json` is the same content with no opinions. Two plans diff cleanly,
which makes "what did this release change about what we have to run" a
question with an answer.

## Read the connection budget before it bites

```text
    held write          1 of 10 for the life of the process (jobs runner 'ingest' LISTEN doorbell); 9 left for requests
```

A [job runner](../../guides/jobs.md) or a message bus holds one `LISTEN`
connection for the life of the process, taken from a workload pool and never
given back. The plan subtracts it, and a pool with nothing left over is reported
as a gap rather than as a row — which is worth knowing before an acquire
timeout with no obvious cause.

See [What does this service actually need?](../../guides/infra.md) for the
camera-trap example inferred end to end, and for what the command deliberately
cannot see.
