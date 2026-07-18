# Manage a database pool with the lifespan

A connection pool should be opened once when your application starts and closed
cleanly when it stops — not created per request, and not leaked on shutdown. The
ASGI lifespan is exactly the place for that, and application state is where the
open pool belongs:

```python
from contextlib import asynccontextmanager
from wreath.postgres import Pool

@asynccontextmanager
async def lifespan(app):
    app.state.pool = Pool(os.environ["DATABASE_URL"])
    await app.state.pool.start()
    try:
        yield
    finally:
        await app.state.pool.close()

app = Wreath(lifespan=lifespan)
```

Run it with `run(app, required_env=["DATABASE_URL"])` so a missing DSN is caught
at boot with a friendly warning, rather than surfacing as a confusing failure on
the first query. Notice the division of labour: the DSN is *configuration*, and
the live pool is *state* — each in its proper place.
