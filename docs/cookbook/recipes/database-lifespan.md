# Manage a database pool with the lifespan

A connection pool should be opened once when your application starts and closed
cleanly when it stops — not created per request, and not leaked on shutdown.
For PostgreSQL, Wreath does this for you: declare the database on the
application and its pools are started during lifespan startup and stopped, with
a grace period, during shutdown:

```python
import os

from wreath import Wreath

app = Wreath()
app.postgres("main", dsn=os.environ["DATABASE_URL"])
```

The database is then available as `app.state.postgres_main`, `app.orm(...)` can
build on it, and handlers reach it through
[session or connection injection](../../guides/orm.md) rather than a global.

For any *other* resource with an open-and-close lifecycle — an outbound queue
client, a metrics exporter, a cache — use the lifespan hooks. Handlers take the
app, run in registration order on startup and in registration order on
shutdown, and application state is where the live resource belongs:

```python
@app.on_startup
async def open_queue(app):
    app.state.queue = await connect_queue(os.environ["QUEUE_URL"])

@app.on_shutdown
async def close_queue(app):
    await app.state.queue.close()
```

Wreath sequences its own resources around yours: databases and ORM registries
are already up before your startup handlers run, and are stopped after your
shutdown handlers finish — so a startup hook may safely use the database, and a
shutdown hook may safely flush to it.

Run it with `run(app, required_env=["DATABASE_URL"])` so a missing DSN is caught
at boot with a friendly warning, rather than surfacing as a confusing failure on
the first query. Notice the division of labour: the DSN is *configuration*, and
the live pool is *state* — each in its proper place.
