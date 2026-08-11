"""Chalkdown seed depot — the aiohttp shapes that have a wreath spelling.

aiohttp is the closest of the foreign frameworks, and the reason is the path
syntax: `/consignments/{consignment_id}` is already what wreath's router
matches, so the decorator survives the port character for character. What
changes is the two ends of the handler signature, and both changes are
mechanical here:

* everything the body reads out of `request` by a constant key is a bound
  parameter — `match_info` is a path parameter, `query` a `Query()`, `headers`
  a `Header()`, `cookies` a `Cookie()`, `await request.json()` a dataclass;
* every `web.json_response(...)` return is a returned value, and every
  `web.HTTP*` raise is a `wreath.exceptions` class of the same status.

The regex converters, `cleanup_ctx` bodies that do not split at the yield, and
the untyped `app["..."]` dict live in `foreign/juniper_depot/`.
"""

from dataclasses import dataclass

from aiohttp import web

routes = web.RouteTableDef()
admin_routes = web.RouteTableDef()


@dataclass
class Consignment:
    species: str
    grams: int
    lot: str


@routes.get("/healthz")
async def healthz(request):
    return web.json_response({"status": "ok"})


@routes.get("/consignments/{consignment_id}")
async def read_consignment(request):
    consignment_id = int(request.match_info["consignment_id"])
    return web.json_response({"consignment": consignment_id})


@routes.get("/consignments/{consignment_id}/lots/{lot}")
async def read_lot(request):
    consignment_id = int(request.match_info["consignment_id"])
    lot = request.match_info["lot"]
    return web.json_response({"consignment": consignment_id, "lot": lot})


@routes.get("/consignments")
async def search_consignments(request):
    species = request.query.get("species")
    limit = int(request.query.get("limit", "20"))
    required_lot = request.query["lot"]
    return web.json_response({"species": species, "limit": limit, "lot": required_lot})


@routes.post("/consignments")
async def create_consignment(request):
    payload = await request.json()
    consignment = Consignment(**payload)
    return web.json_response({"lot": consignment.lot}, status=201)


@routes.post("/consignments/{consignment_id}/weigh")
async def weigh_consignment(request):
    form = await request.post()
    grams = int(form["grams"])
    return web.json_response({"grams": grams})


@routes.get("/consignments/{consignment_id}/trace")
async def trace_consignment(request):
    trace = request.headers.get("X-Trace-Id")
    session = request.cookies.get("depot_session")
    return web.json_response({"trace": trace, "session": session})


@routes.get("/consignments/{consignment_id}/label")
async def label_consignment(request):
    return web.Response(text="chalkdown/seed/label")


@routes.delete("/consignments/{consignment_id}")
async def drop_consignment(request):
    return web.Response(status=204)


@routes.get("/consignments/{consignment_id}/manifest")
async def manifest(request):
    consignment_id = request.match_info["consignment_id"]
    if consignment_id == "0":
        raise web.HTTPNotFound(reason="no such consignment")
    if consignment_id == "1":
        raise web.HTTPConflict()
    return web.json_response({"consignment": consignment_id})


@routes.get("/consignments/{consignment_id}/certificate")
async def certificate(request):
    return web.FileResponse("/srv/chalkdown/certificate.pdf")


@admin_routes.get("/audit")
async def audit(request):
    return web.json_response({"entries": []})


@web.middleware
async def request_id(request, handler):
    response = await handler(request)
    response.headers["X-Request-Id"] = "chalkdown"
    return response


async def open_pool(app):
    app["pool"] = await connect_pool()


async def close_pool(app):
    await app["pool"].close()


async def seed_index(app):
    app["index"] = build_index()
    yield
    app["index"].clear()


def make_app():
    app = web.Application(middlewares=[request_id])
    app.add_routes(routes)
    app.on_startup.append(open_pool)
    app.on_cleanup.append(close_pool)
    app.cleanup_ctx.append(seed_index)
    admin = web.Application()
    admin.add_routes(admin_routes)
    app.add_subapp("/admin", admin)
    return app


async def connect_pool():
    raise NotImplementedError("wired up by the runner")


def build_index():
    raise NotImplementedError("wired up by the runner")
