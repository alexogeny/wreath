"""Webhook relay for a co-operative of small suppliers.

The endpoint list is not in this file. Each supplier row carries a slug, and the
route for it is registered at startup by a loop over those rows, with the handler
built by a closure. Grep the source for any path this service answers on and you
will find nothing: the URL space exists only once the process has read the
database.

This is the shape that defeats enumeration. A static analyzer can see
`add_post` being called; it cannot know how many times, or with what.
"""

from aiohttp import web

routes = web.RouteTableDef()


def make_handler(supplier):
    async def handle(request):
        payload = await request.json()
        await forward(request.app, supplier, payload)
        return web.json_response({"accepted": True}, status=202)

    return handle


async def forward(app, supplier, payload):
    session = app["session"]
    for target in app["subscribers"].get(supplier["slug"], ()):
        async with session.post(target, json=payload) as response:
            await response.read()


async def load_suppliers(app):
    rows = await app["db"].fetch("SELECT slug, name FROM supplier WHERE active")
    for row in rows:
        supplier = dict(row)
        # Registered here, named nowhere.
        app.router.add_post(f"/hook/{supplier['slug']}", make_handler(supplier))


@routes.get("/healthz")
async def healthz(request):
    return web.json_response({"status": "ok"})


def build():
    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(load_suppliers)
    return app
