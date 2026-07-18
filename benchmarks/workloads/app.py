"""A neutral workload application exercising all seven workload shapes.

Every shape is expressed through public Wreath APIs only — no benchmark-specific
routes, table names, constants, or randomization live here. A conformance
adapter maps these generic endpoints to prescribed names externally.

Shapes:

1. Small JSON serialization         GET  /json
2. Reusable static/plaintext        GET  /plaintext
3. Point database read              GET  /widget/{id}
4. Independent fan-out reads        GET  /widgets?queries=N
5. Transactional read-modify-write  POST /widgets/update
6. Escaped template table render    GET  /quotations
7. Snapshot-cache read              GET  /config/{key}

The database-backed shapes are registered only when a database is supplied, so
the app also runs (and is verifiable) for the pure in-process shapes alone.
"""

from __future__ import annotations

from typing import Annotated, Any

from wreath import JSONResponse, Wreath
from wreath.binding import Query
from wreath.cache import SnapshotCache
from wreath.response import HTMLResponse, PreparedResponse
from wreath.templates import Template

# Compiled once at import; rendering is the request-time path.
QUOTATION_TABLE = Template.from_string(
    "<table>\n"
    "{% for row in rows %}"
    "<tr><td>{{ row.id }}</td><td>{{ row.message }}</td></tr>\n"
    "{% endfor %}"
    "</table>"
)

PLAINTEXT = PreparedResponse.text("Hello, World!")


def build_app(dsn: str | None = None) -> Wreath:
    app = Wreath()

    # A read-mostly application cache published atomically at startup.
    config_cache: SnapshotCache[str, str] = SnapshotCache()
    app.state.config_cache = config_cache

    @app.on_startup
    async def _load_config(_: Wreath) -> None:
        config_cache.replace({"greeting": "Hello, World!", "edition": "neutral"})

    @app.get("/json")
    async def small_json(request: Any) -> Any:
        # Shape 1: small JSON document.
        return JSONResponse({"message": "Hello, World!"})

    @app.get("/plaintext")
    async def plaintext(request: Any) -> Any:
        # Shape 2: one prebuilt immutable response reused by every request.
        return PLAINTEXT

    @app.get("/config/{key}")
    async def config_read(request: Any, key: Annotated[str, Query()] = "greeting") -> Any:
        # Shape 7: snapshot-cache read; an explicit miss, never hidden I/O.
        value = config_cache.get(request.path_params["key"])
        if value is None:
            return JSONResponse({"error": "unknown key"}, status=404)
        return JSONResponse({"key": request.path_params["key"], "value": value})

    if dsn is not None:
        # The database is lifespan-managed: pools start on startup, stop on
        # shutdown. Statements are registered (prepared) here, at startup.
        database = app.postgres("workloads", dsn=dsn)
        _register_database_shapes(app, database)

    return app


def _register_database_shapes(app: Wreath, database: Any) -> None:
    get_widget = database.statement(
        "widget.get", 'SELECT id, value FROM "widget" WHERE id = $1'
    )
    update_widget = database.statement(
        "widget.update",
        'UPDATE "widget" SET value = $1 WHERE id = $2',
        workload="write",
    )
    list_quotations = database.statement(
        "quotation.list", 'SELECT id, message FROM "quotation" ORDER BY id'
    )

    @app.get("/widget/{id}")
    async def widget_read(request: Any, id: int) -> Any:
        # Shape 3: point read through a startup-prepared statement.
        row = await get_widget.fetchrow(id)
        if row is None:
            return JSONResponse({"error": "not found"}, status=404)
        return JSONResponse({"id": row["id"], "value": row["value"]})

    @app.get("/widgets")
    async def widget_fanout(
        request: Any,
        queries: Annotated[int, Query(minimum=1, maximum=500, overflow="clamp")] = 1,
    ) -> Any:
        # Shape 4: independent fan-out reads, one operation per id, ordered.
        ids = [(index + 1,) for index in range(queries)]
        rows = await get_widget.map("fetchrow", ids, max_in_flight=32)
        return JSONResponse(
            [None if row is None else {"id": row["id"], "value": row["value"]} for row in rows]
        )

    @app.post("/widgets/update")
    async def widget_update(
        request: Any,
        queries: Annotated[int, Query(minimum=1, maximum=500, overflow="clamp")] = 1,
    ) -> Any:
        # Shape 5: transactional read-modify-write. Reads complete before the
        # dependent writes; the application chooses the new values.
        body = await request.json()
        updates = body["updates"][:queries]
        connection = await database.acquire("write")
        try:
            async with connection.transaction() as tx:
                read_ids = [(item["id"],) for item in updates]
                rows = await tx.map("fetchrow", get_widget.sql, read_ids)
                write_args = [(item["value"], item["id"]) for item in updates]
                await tx.map("execute", update_widget.sql, write_args)
        finally:
            await database.release("write", connection)
        return JSONResponse({"updated": len(updates), "read": len(rows)})

    @app.get("/quotations")
    async def quotations(request: Any) -> Any:
        # Shape 6: fetch a collection, add one request-created row, sort in
        # application code, and render an escaped HTML table.
        rows = await list_quotations.fetch()
        collection = [{"id": row["id"], "message": row["message"]} for row in rows]
        collection.append({"id": 0, "message": "Additional <fortune> & \"quote\""})
        collection.sort(key=lambda item: item["message"])
        return HTMLResponse(QUOTATION_TABLE.render(rows=collection))
