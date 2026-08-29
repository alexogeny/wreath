"""Juniper transit depot — the aiohttp shapes that stay refused.

The companion to `corpus/chalkdown_depot/`. Same framework, same spellings, and
none of it is a rewrite:

* `{code:[A-Z]{2}}` — `_routing.check_placeholders` raises on any converter but
  `path`: "unknown path converter ...; the only converter is 'path'". A regex
  placeholder has no target, and downgrading it to `{code}` widens the route.
* `match_info.get(..., default)` — `binding.Path` says "A path parameter is
  always required and its handler default is never consulted". A defaulted read
  of a path segment means the route is really two routes.
* the dynamic route table — the set of endpoints is not in the source.
* `cleanup_ctx` whose body does not split at the `yield` — the acquire and the
  release share a `try`, so there is no pair of `on_startup`/`on_shutdown`
  handlers that reproduces it.
* `StreamResponse` with `prepare()`/`write()` — the handler drives the socket.
  `wreath.response.StreamingResponse` consumes an iterator instead, so the
  control flow inverts.
"""

import contextlib

from aiohttp import web

routes = web.RouteTableDef()

TERMINALS = {"north": "/depots/north", "south": "/depots/south"}


@routes.get("/wagons/{code:[A-Z]{2}[0-9]{4}}")
async def read_wagon(request):
    return web.json_response({"code": request.match_info["code"]})


@routes.get("/manifests/{manifest_id}")
async def read_manifest(request):
    revision = request.match_info.get("revision", "latest")
    return web.json_response({"revision": revision})


@routes.get("/tail")
async def tail_movements(request):
    response = web.StreamResponse()
    await response.prepare(request)
    async for line in movements():
        await response.write(line)
    await response.write_eof()
    return response


async def depot_pool(app):
    connection = await connect()
    with contextlib.suppress(Exception):
        try:
            app["pool"] = connection
            yield
        finally:
            await connection.close()
            await drain_audit(connection)


def make_app():
    app = web.Application()
    app.add_routes(routes)
    for name, path in TERMINALS.items():
        app.router.add_get(path, terminal_handler(name))
    app.cleanup_ctx.append(depot_pool)
    return app


def terminal_handler(name):
    async def handler(request):
        return web.json_response({"terminal": name})

    return handler


async def movements():
    raise NotImplementedError("wired up by the runner")


async def connect():
    raise NotImplementedError("wired up by the runner")


async def drain_audit(connection):
    raise NotImplementedError("wired up by the runner")
