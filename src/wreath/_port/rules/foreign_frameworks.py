"""Frameworks this tool recognizes and never translates. See `.._port.foreign`
for the detection that fires them.
"""

from __future__ import annotations

from ..ir import TRANSLATED, UNSUPPORTED

#: Foreign constructs with an exact wreath spelling, which the emitter writes.
#:
#: These are keyed by *construct* rather than by framework, unlike the refusals
#: below. Five frameworks say "404" five ways and all five become
#: `raise NotFound()`, so one rule with one message is the honest shape -- a rule
#: per framework would be five copies of one sentence, and the fifth spelling is
#: always the one nobody remembers to add.
#:
#: The category stays `foreign` whatever the verdict. That is load-bearing: the
#: `routing`/`params`/`exceptions` categories are the *FastAPI* rules, and a
#: Bottle application once scored 1.00 coverage because `route.method` fired on a
#: decorator it had never identified. Keeping every foreign finding in its own
#: category makes "no routing finding from a foreign root" structural rather than
#: something a later rule can quietly break.
FOREIGN_TRANSLATED: dict[str, tuple[str, str, str, str]] = {
    "port.http.exception": (
        "http_error",
        "foreign",
        TRANSLATED,
        "This becomes `raise <Class>()` from wreath.exceptions -- the status is a class attribute, so there is no abort() and no status argument. The detail is the first positional argument. Tornado's second positional is a log line rather than a reason and is deliberately not carried across.",
    ),
    "port.app.wreath": (
        "app",
        "foreign",
        TRANSLATED,
        "Flask(__name__) / Bottle() becomes Wreath(). Wreath takes no positional argument and every parameter is keyword-only, so the import-name argument has nowhere to go and needs none: wreath does not locate templates or static files from it.",
    ),
    "port.router.new": (
        "router",
        "foreign",
        TRANSLATED,
        "A Blueprint or RouteTableDef becomes Router(prefix=..., tags=(...)). Only where it registers no before_request/errorhandler of its own -- those are re-declared on the application rather than carried, so a blueprint with hooks is not this.",
    ),
    "port.router.include": (
        "router",
        "foreign",
        TRANSLATED,
        "register_blueprint(x) / add_routes(x) / add_subapp(prefix, x) all become app.include_router(x[, prefix=...]).",
    ),
    "port.route.method": (
        "route",
        "foreign",
        TRANSLATED,
        "The decorator becomes the wreath verb for the methods it names, the path's placeholders become {name}, and each captured parameter gains the annotation its converter implied. A GET route answers HEAD as well, which is what Flask's default methods meant. The handler keeps its `def` -- wreath runs a synchronous handler natively, so nothing here puts blocking I/O onto the event loop.",
    ),
    "port.http.redirect": (
        "http_redirect",
        "foreign",
        TRANSLATED,
        "This becomes `return RedirectResponse(url, status=...)`. The status is never dropped: wreath's default is 307 and every one of these is 301/302/303, so leaving it off turns a permanent redirect into a temporary one and a GET-after-POST into a re-POST.",
    ),
}

FOREIGN: dict[str, tuple[str, str, str, str]] = {
    # Recognized, never translated. Without these a Tornado application reports
    # `0 translated, 0 needs-review, 0 unsupported` -- the numbers an empty
    # directory produces -- and the size of the job stays invisible. Counting
    # them is how "I cannot port this" becomes a quantity instead of a silence.
    # Each fires only where its framework is imported: `@app.route(...)` is
    # spelled the same in Flask and Bottle, and aiohttp's `@routes.get` is
    # FastAPI's exactly.
    "foreign.flask.app": (
        "flask_app",
        "foreign",
        UNSUPPORTED,
        "Flask() has no mechanical equivalent. Wreath() is an ASGI application; Flask is WSGI, and the handler signature differs -- Flask reads the request from a module-level proxy, wreath takes it as a parameter.",
    ),
    "foreign.flask.blueprint": (
        "blueprint",
        "foreign",
        UNSUPPORTED,
        "A Flask Blueprint is close in spirit to Router, but url_prefix, error handlers and before_request hooks are re-declared rather than translated. Port the routes first, then re-attach the hooks.",
    ),
    "foreign.flask.route": (
        "flask_route",
        "foreign",
        UNSUPPORTED,
        "A Flask route decorator. The path syntax (`<int:id>`) is not wreath's, and the handler takes no request parameter -- it reads a module-level proxy. Rewrite by hand.",
    ),
    "foreign.flask.hook": (
        "flask_hook",
        "foreign",
        UNSUPPORTED,
        "A Flask request hook. These run against the `g`/`request` proxies, which have no equivalent: wreath passes the request explicitly, so what the hook stashed on `g` becomes a parameter or middleware state.",
    ),
    "foreign.bottle.app": (
        "bottle_app",
        "foreign",
        UNSUPPORTED,
        "Bottle() has no mechanical equivalent, and Bottle is WSGI. Rewrite the application object by hand.",
    ),
    "foreign.bottle.route": (
        "bottle_route",
        "foreign",
        UNSUPPORTED,
        "A Bottle route decorator. Spelled like FastAPI's and not the same thing: the handler reads a module-level request proxy and returns a body rather than a response.",
    ),
    "foreign.aiohttp.app": (
        "aiohttp_app",
        "foreign",
        UNSUPPORTED,
        "web.Application() carries its state in a string-keyed dict and its lifecycle in on_startup/cleanup_ctx. Both become explicit in wreath; neither converts on its own.",
    ),
    "foreign.aiohttp.route": (
        "aiohttp_route",
        "foreign",
        UNSUPPORTED,
        "An aiohttp route decorator. The handler takes the request and returns a web.Response, so both ends of the signature change.",
    ),
    "foreign.aiohttp.route_dynamic": (
        "aiohttp_route_dynamic",
        "foreign",
        UNSUPPORTED,
        "A route registered by calling the router rather than by decorating. If this call is inside a loop or reads configuration, the set of endpoints this service answers on does not exist in the source at all -- no static tool can enumerate it, and the list has to come from the running application.",
    ),
    "foreign.aiohttp.middleware": (
        "aiohttp_middleware",
        "foreign",
        UNSUPPORTED,
        "An aiohttp middleware. The signature (handler passed in, response returned) and the ordering rules both differ from wreath's.",
    ),
    "foreign.tornado.handler": (
        "tornado_handler",
        "foreign",
        UNSUPPORTED,
        "A Tornado RequestHandler. Behaviour is inherited: which mixins this class lists decides whether it authenticates, and `prepare`/`on_finish` run around every method. A wreath route is a function, so the hierarchy has to be flattened by hand.",
    ),
    "foreign.tornado.websocket": (
        "tornado_websocket",
        "foreign",
        UNSUPPORTED,
        "A Tornado WebSocketHandler. Class-level client registries and open/on_message/on_close do not map onto wreath's websocket function.",
    ),
    "foreign.tornado.routes": (
        "tornado_routes",
        "foreign",
        UNSUPPORTED,
        "Tornado routing is a list of (regex, handler) tuples, with captures arriving as positional strings. Path parameters, their names and their types all have to be reconstructed from the pattern.",
    ),
    "foreign.tornado.coroutine": (
        "gen_coroutine",
        "foreign",
        UNSUPPORTED,
        "A @gen.coroutine. It executes eagerly, so it half-works when called without being awaited -- and a mechanical rewrite to `async def` turns that call into a coroutine nobody awaits, which is a silently missing effect rather than an error. Check every caller before converting.",
    ),
    "foreign.pyramid.config": (
        "configurator",
        "foreign",
        UNSUPPORTED,
        "A Pyramid Configurator. Views are attached by scanning for decorators at configuration time, so what this application serves is decided at startup rather than declared in the source.",
    ),
    "foreign.pyramid.view": (
        "view_config",
        "foreign",
        UNSUPPORTED,
        "A Pyramid view. It is bound to a resource *type* rather than a path, and which view wins can depend on scan order, so the URL it answers on is not written here.",
    ),
    "foreign.pyramid.route": (
        "pyramid_route",
        "foreign",
        UNSUPPORTED,
        "A Pyramid URL-dispatch route. This is the half of Pyramid that has paths; the traversal half does not, and an application using both serves some resources by two routes with different authorization.",
    ),
    "foreign.pyramid.traversal": (
        "traversal",
        "foreign",
        UNSUPPORTED,
        "Traversal: the URL is walked one segment at a time through __getitem__, so this application's URL space is an object graph built from data, not a set of patterns. No static analysis can enumerate it -- take the list from the running application instead.",
    ),
    "foreign.pyramid.acl": (
        "acl",
        "foreign",
        UNSUPPORTED,
        "A Pyramid ACL, inherited down the resource lineage unless a level resets it. Authorization here is a property of position in the tree, which has no equivalent in a decorator on a function.",
    ),
    "foreign.django.model": (
        "django_model",
        "foreign",
        UNSUPPORTED,
        "A Django model. Fields, Meta, and any logic in save() all move, and `objects` is usually a manager that filters rows out -- so a query rewritten as a plain select silently widens.",
    ),
    "foreign.django.manager": (
        "django_manager",
        "foreign",
        UNSUPPORTED,
        "A Django manager or queryset. Its get_queryset() is an implicit predicate on every `.objects` call in the codebase, and it appears at none of those call sites.",
    ),
    "foreign.django.query": (
        "django_query",
        "foreign",
        UNSUPPORTED,
        "A query through a Django manager. `objects` is not every row -- whatever get_queryset() filters out is a predicate this line does not show, so rewriting the verb alone widens the query for exactly the rows somebody meant to hide. Read the manager before porting any of these; the verb is the easy half.",
    ),
    "foreign.django.drf": (
        "drf_class",
        "foreign",
        UNSUPPORTED,
        "A Django REST Framework viewset or serializer. Routers generate the URLs, so the endpoint list is computed rather than declared; serializer fields are not pydantic fields.",
    ),
    "foreign.django.admin": (
        "django_admin",
        "foreign",
        UNSUPPORTED,
        "A Django admin registration. There is no wreath equivalent -- the admin is a whole application, and anything staff rely on there has to be rebuilt as ordinary routes.",
    ),
    "foreign.flask.proxy": (
        "flask_proxy",
        "foreign",
        UNSUPPORTED,
        "`g` / `current_app` are per-request proxies resolved from a context stack. Wreath passes the request explicitly, so anything stashed on `g` becomes a parameter, middleware state or a contextvar -- and which one is a design decision, not a rename.",
    ),
    "foreign.tornado.method": (
        "handler_method",
        "foreign",
        UNSUPPORTED,
        "A handler verb. This is an endpoint: a class with get/post/delete is three routes, and its path comes from the regex tuple that names the class, not from anything written here.",
    ),
    "foreign.tornado.hook": (
        "handler_hook",
        "foreign",
        UNSUPPORTED,
        "prepare/on_finish/initialize run around every verb on this handler and are inherited, so the effective behaviour of a route is spread across its whole base-class chain.",
    ),
    "foreign.tornado.authenticated": (
        "authenticated",
        "foreign",
        UNSUPPORTED,
        "@authenticated redirects rather than raising, and calls get_current_user() -- which in most Tornado applications is a synchronous query on the IOLoop. Both halves change on the way across.",
    ),
    "foreign.tornado.periodic": (
        "periodic_callback",
        "foreign",
        UNSUPPORTED,
        "In-process scheduling on the IOLoop. It runs in every replica and has no coordination, so what it becomes depends on whether the job may run more than once.",
    ),
    "foreign.tornado.options": (
        "tornado_options",
        "foreign",
        UNSUPPORTED,
        "tornado.options.define() declares a process-global read from module scope at import time. Wreath's configuration is an object bound at startup, so both the declaration and every read site move.",
    ),
    "foreign.aiohttp.lifecycle": (
        "aiohttp_lifecycle",
        "foreign",
        UNSUPPORTED,
        "on_startup/on_cleanup/cleanup_ctx hold this application's resources. Wreath uses a lifespan context, and the ordering guarantees are not the same -- cleanup_ctx unwinds in reverse, appended handlers do not.",
    ),
    "foreign.aiohttp.response": (
        "aiohttp_response",
        "foreign",
        UNSUPPORTED,
        "An aiohttp response object. Handlers return these explicitly; a wreath handler returns a value and the framework builds the response, so every return site changes shape.",
    ),
    "foreign.aiohttp.client": (
        "client_session",
        "foreign",
        UNSUPPORTED,
        "A ClientSession. Its lifetime is bound to an event loop, which is why one built at import time works under run_app and fails under a preloading worker. Where it is created is part of what has to be ported.",
    ),
    "foreign.aiohttp.subapp": (
        "add_subapp",
        "foreign",
        UNSUPPORTED,
        "A mounted sub-application. Its middleware and lifecycle are separate from the parent's, which a flat Router does not reproduce.",
    ),
    "foreign.aiohttp.state": (
        "app_state",
        "foreign",
        UNSUPPORTED,
        "String-keyed application state. Nothing declares these keys or their types, so what a handler can rely on is only discoverable by reading every writer.",
    ),
    "foreign.pyramid.tween": (
        "tween",
        "foreign",
        UNSUPPORTED,
        "A tween, ordered explicitly against other tweens. Middleware ordering in wreath is registration order, so an over/under constraint has to be resolved into a position by hand.",
    ),
    "foreign.pyramid.include": (
        "config_include",
        "foreign",
        UNSUPPORTED,
        "config.include() pulls another package's views, routes and tweens into this application at startup. What it added is not visible here, and load order decides conflicts.",
    ),
    "foreign.pyramid.registry": (
        "registry_lookup",
        "foreign",
        UNSUPPORTED,
        "A component-registry lookup: the implementation is chosen at runtime by interface, and which one wins depends on configuration order. There is no static answer to what this returns.",
    ),
    "foreign.gevent.spawn": (
        "gevent_spawn",
        "foreign",
        UNSUPPORTED,
        "A spawned greenlet. Unbounded unless a pool bounds it, and an exception in one dies silently unless something joins it -- neither of which survives a rename to create_task.",
    ),
    "foreign.gevent.pool": (
        "gevent_pool",
        "foreign",
        UNSUPPORTED,
        "A greenlet pool. Its bound is the only backpressure in the fan-out it feeds; asyncio's equivalent is a semaphore, and the sizing is not transferable.",
    ),
    "foreign.gevent.timeout": (
        "gevent_timeout",
        "foreign",
        UNSUPPORTED,
        "gevent.Timeout derives from BaseException, so an `except Exception` around it does not catch it and the greenlet dies. asyncio.timeout raises TimeoutError, which that same handler *does* catch -- the behaviour inverts on the way across.",
    ),
    "foreign.gevent.threading": (
        "patched_threading",
        "foreign",
        UNSUPPORTED,
        "threading.local/Lock under monkeypatching is greenlet-local and a greenlet lock. It works by accident and gives none of the protection the code around it assumes; deciding what it should have been is a design question.",
    ),
    "foreign.gevent.session": (
        "shared_session",
        "foreign",
        UNSUPPORTED,
        "A requests.Session shared across greenlets. Connection and cookie state is shared with it, which is a correctness question before it is a porting one.",
    ),
    "foreign.flask.api": (
        "flask_api",
        "foreign",
        UNSUPPORTED,
        "Flask API used here. Wreath has no mechanical equivalent for it; the call has to be re-expressed.",
    ),
    "foreign.aiohttp.api": (
        "aiohttp_api",
        "foreign",
        UNSUPPORTED,
        "aiohttp API used here. Wreath has no mechanical equivalent for it; the call has to be re-expressed.",
    ),
    "foreign.tornado.api": (
        "tornado_api",
        "foreign",
        UNSUPPORTED,
        "Tornado API used here. Wreath has no mechanical equivalent for it; the call has to be re-expressed.",
    ),
    "foreign.pyramid.api": (
        "pyramid_api",
        "foreign",
        UNSUPPORTED,
        "Pyramid API used here. Wreath has no mechanical equivalent for it; the call has to be re-expressed.",
    ),
    "foreign.bottle.api": (
        "bottle_api",
        "foreign",
        UNSUPPORTED,
        "Bottle API used here. Wreath has no mechanical equivalent for it; the call has to be re-expressed.",
    ),
    "foreign.django.api": (
        "django_api",
        "foreign",
        UNSUPPORTED,
        "Django API used here. Wreath has no mechanical equivalent for it; the call has to be re-expressed.",
    ),
    "foreign.gevent.api": (
        "gevent_api",
        "foreign",
        UNSUPPORTED,
        "gevent/eventlet API used here. Its semantics depend on the monkeypatch, so it cannot be renamed across.",
    ),
    "foreign.tornado.inherited": (
        "handler_api",
        "foreign",
        UNSUPPORTED,
        "Framework API reached through `self`. A Tornado handler inherits its request, its response and its arguments from RequestHandler, so no import in this module mentions any of it -- and every one of these calls has to become something explicit when the class becomes a function.",
    ),
    "foreign.django.inherited": (
        "model_api",
        "foreign",
        UNSUPPORTED,
        "Framework API reached through `self` on a model, manager or serializer -- save(), pk, validated_data and the rest are inherited, so no import in this module names them. Each is a behaviour the base class supplied and something has to supply again.",
    ),
    "foreign.aiohttp.request": (
        "request_api",
        "foreign",
        UNSUPPORTED,
        "aiohttp Request/Application API on a handler parameter. match_info, config_dict and the app dict have no direct equivalent, and nothing in the signature declares the type -- so this is the surface a reader of the imports never sees.",
    ),
    "foreign.pyramid.request": (
        "request_api",
        "foreign",
        UNSUPPORTED,
        "Pyramid request/context/config API on a view parameter. The context is the traversed resource, so what this attribute means depends on where in the resource tree the URL landed.",
    ),
    "foreign.gevent.blocking": (
        "blocking_io",
        "foreign",
        UNSUPPORTED,
        "Blocking I/O under a monkeypatch. Whether this call yields was decided by patch_all(), not by the call site -- and psycopg2, a C extension, never yields at all, so it stalls every greenlet in the worker. This is the call that has to be replaced, not renamed.",
    ),
    "foreign.gevent.monkeypatch": (
        "monkeypatch",
        "foreign",
        UNSUPPORTED,
        "monkey.patch_all() reinterprets every blocking call beneath it as a yield point. Nothing in this tree can be ported mechanically: a rewrite to `async def` produces code that passes its tests at low concurrency and serialises in production. Decide the concurrency model first, by hand.",
    ),
}
