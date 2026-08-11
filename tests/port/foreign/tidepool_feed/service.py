"""Live readings from a rockpool sensor network.

Half of what this service answers on is decorated here and half is registered in
a loop over stored configuration, so the URL set does not exist in the source at
all. Its resources live in a string-keyed application dict that nothing declares,
its lifecycle is a pair of append-ordered callbacks, and every handler both takes
a request object and returns a response object — so both ends of every signature
change on the way across.
"""
from aiohttp import ClientSession, web

routes = web.RouteTableDef()


@routes.get("/pools/{pool}")
async def read_pool(request):
    pool = request.match_info["pool"]
    store = request.app["pool_store"]
    return web.json_response(await store.latest(pool))


@routes.post("/pools/{pool}/samples")
async def record_sample(request):
    payload = await request.json()
    await request.app["pool_store"].append(request.match_info["pool"], payload)
    return web.Response(status=202)


@web.middleware
async def stamp_survey(request, handler):
    response = await handler(request)
    response.headers["X-Survey"] = request.app["survey_id"]
    return response


async def open_client(app):
    app["client"] = ClientSession(raise_for_status=True)


async def close_client(app):
    await app["client"].close()


def build(config):
    app = web.Application(middlewares=[stamp_survey])
    app.add_routes(routes)
    for legacy in config["legacy_pools"]:
        app.router.add_get(f"/legacy/{legacy['slug']}", read_pool)
    app.on_startup.append(open_client)
    app.on_cleanup.append(close_client)
    app.add_subapp("/admin/", web.Application())
    return app
