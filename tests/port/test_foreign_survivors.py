from __future__ import annotations

import ast
from collections import Counter

from wreath._port.analyzer.imports import _Imports
from wreath._port.foreign import foreign_findings


def _findings(
    source: str,
    roots: set[str],
    *,
    class_bases: dict[str, list[str]] | None = None,
    class_members: dict[str, set[str]] | None = None,
    with_imports: bool = False,
) -> Counter[tuple[int, str]]:
    tree = ast.parse(source)
    imports = _Imports().visit(tree) if with_imports else None
    return Counter(
        (finding.line, finding.rule_id)
        for finding in foreign_findings(
            "app.py",
            tree,
            frozenset(roots),
            class_bases,
            imports,
            class_members,
        )
    )


def test_flask_rules_are_exactly_gated() -> None:
    source = """\
@app.get("/ok")
def translated(): pass
@app.route(path)
def dynamic(): pass
@app.before_request
def hook(): pass
@app.middleware
def not_a_flask_hook(): pass
Flask()
Blueprint("plain")
Blueprint(name)
register_blueprint(router)
register_blueprint(router, url_prefix="/x")
Bottle()
RouteTableDef()
add_routes(router)
app.router.add_get("/x", handler)
add_subapp("/x", child)
web.Application()
ClientSession()
web.Response()
Application()
PeriodicCallback(tick)
add_route("x", "/x")
add_tween("x")
include("x")
Configurator()
g.user
current_app.name
app["state"]
abort(404)
redirect("/x", code=302)
"""

    assert _findings(source, {"flask"}) == Counter(
        {
            (1, "port.route.method"): 1,
            (3, "foreign.flask.route"): 1,
            (5, "foreign.flask.hook"): 1,
            (9, "port.app.wreath"): 1,
            (10, "port.router.new"): 1,
            (11, "foreign.flask.blueprint"): 1,
            (12, "port.router.include"): 1,
            (16, "port.router.include"): 1,
            (28, "foreign.flask.proxy"): 1,
            (29, "foreign.flask.proxy"): 1,
            (31, "port.http.exception"): 1,
            (32, "port.http.redirect"): 1,
        }
    )


def test_bottle_rules_distinguish_plain_and_monkeypatched_apps() -> None:
    source = """\
@app.get("/ok")
def translated(): pass
@app.route(path)
def dynamic(): pass
Bottle()
Flask()
abort(409, "clash")
redirect("/next", 303)
"""

    assert _findings(source, {"bottle"}) == Counter(
        {
            (1, "port.route.method"): 1,
            (3, "foreign.bottle.route"): 1,
            (5, "port.app.wreath"): 1,
            (7, "port.http.exception"): 1,
            (8, "port.http.redirect"): 1,
        }
    )
    assert _findings("Bottle()\n", {"bottle", "gevent"}) == Counter({(1, "foreign.bottle.app"): 1})


def test_aiohttp_rules_require_their_full_shapes() -> None:
    source = """\
@routes.get("/ok")
async def translated(request): pass
@routes.route(path)
async def dynamic(request): pass
@web.middleware
async def middleware(request, handler): pass
RouteTableDef()
add_routes(routes)
add_routes(first, second)
add_routes(routes, prefix="/x")
app.router.add_get("/x", handler)
app.add_get("/x", handler)
add_subapp("/x", child)
web.Application()
Application()
ClientSession()
web.Response()
Response()
PeriodicCallback(tick)
app.on_startup
app.other
g.user
app["state"]
other["state"]
app[key]
app[1]
async def handler(request, other):
    request.match_info
    other.match_info
"""

    assert _findings(source, {"aiohttp"}) == Counter(
        {
            (1, "port.route.method"): 1,
            (3, "foreign.aiohttp.route"): 1,
            (5, "foreign.aiohttp.middleware"): 1,
            (7, "port.router.new"): 1,
            (8, "port.router.include"): 1,
            (11, "foreign.aiohttp.route_dynamic"): 1,
            (13, "foreign.aiohttp.subapp"): 1,
            (14, "foreign.aiohttp.app"): 1,
            (16, "foreign.aiohttp.client"): 1,
            (17, "foreign.aiohttp.response"): 1,
            (20, "foreign.aiohttp.lifecycle"): 1,
            (23, "foreign.aiohttp.state"): 1,
            (28, "foreign.aiohttp.request"): 1,
        }
    )


def test_tornado_rules_cover_decorators_handlers_and_calls() -> None:
    source = """\
@gen.coroutine
def old_style(): pass
@authenticated
def secure(): pass
@view_config(route_name="x")
def not_pyramid(): pass
class Handler(BaseHandler):
    def get(self):
        self.write("ok")
        self.local()
    def prepare(self): pass
    def local(self): pass
class Socket(WebSocketHandler):
    def open(self):
        self.write_message("ok")
Application([])
PeriodicCallback(tick, 1000)
add_timeout(deadline, tick)
call_later(1, tick)
options.define("x")
define("x")
add_route("x", "/x")
"""
    class_bases = {
        "BaseHandler": ["RequestHandler"],
        "Handler": ["BaseHandler"],
        "Socket": ["WebSocketHandler"],
    }
    class_members = {"Handler": {"get", "prepare", "local"}, "Socket": {"open"}}

    assert _findings(
        source,
        {"tornado"},
        class_bases=class_bases,
        class_members=class_members,
    ) == Counter(
        {
            (1, "foreign.tornado.coroutine"): 1,
            (3, "foreign.tornado.authenticated"): 1,
            (7, "foreign.tornado.handler"): 1,
            (8, "foreign.tornado.method"): 1,
            (9, "foreign.tornado.inherited"): 1,
            (11, "foreign.tornado.hook"): 1,
            (13, "foreign.tornado.websocket"): 1,
            (15, "foreign.tornado.inherited"): 1,
            (16, "foreign.tornado.routes"): 1,
            (17, "foreign.tornado.periodic"): 1,
            (18, "foreign.tornado.periodic"): 1,
            (19, "foreign.tornado.periodic"): 1,
            (20, "foreign.tornado.options"): 1,
        }
    )


def test_pyramid_rules_distinguish_traversal_acl_and_configuration() -> None:
    source = """\
@view_config(route_name="x")
def view(request):
    request.registry
    local.registry
@subscriber(object)
def event(context):
    context.__parent__
@view_defaults(renderer="json")
class Views:
    __acl__ = [(Allow, Everyone, "view")]
    other = 1
    def __getitem__(self, key): return key
    async def not_traversal(self): pass
__acl__ = []
other = []
add_route("x", "/x")
add_view(view, route_name="x")
add_tween("pkg.tween")
include("pkg.routes")
Configurator()
app.registry
app.on_startup
async def handler(config, unrelated):
    config.registry
    unrelated.registry
"""

    assert _findings(source, {"pyramid"}) == Counter(
        {
            (1, "foreign.pyramid.view"): 1,
            (3, "foreign.pyramid.request"): 1,
            (3, "foreign.pyramid.registry"): 1,
            (4, "foreign.pyramid.registry"): 1,
            (5, "foreign.pyramid.view"): 1,
            (7, "foreign.pyramid.request"): 1,
            (8, "foreign.pyramid.view"): 1,
            (10, "foreign.pyramid.acl"): 1,
            (12, "foreign.pyramid.traversal"): 1,
            (14, "foreign.pyramid.acl"): 1,
            (16, "foreign.pyramid.route"): 1,
            (17, "foreign.pyramid.route"): 1,
            (18, "foreign.pyramid.tween"): 1,
            (19, "foreign.pyramid.include"): 1,
            (20, "foreign.pyramid.config"): 1,
            (21, "foreign.pyramid.registry"): 1,
            (24, "foreign.pyramid.request"): 1,
            (24, "foreign.pyramid.registry"): 1,
            (25, "foreign.pyramid.registry"): 1,
        }
    )


def test_django_rules_cover_model_family_and_field_shapes() -> None:
    source = """\
class Row(models.Model):
    name = models.CharField(max_length=10)
    owner = models.ForeignKey(User, on_delete=CASCADE)
    peers = models.ManyToManyField("self")
    odd = models.DurationField()
    value = helper()
    label = "plain"
class Rows(models.Manager): pass
class Query(models.QuerySet): pass
class Api(ModelSerializer): pass
class View(CustomAPIView): pass
class Plain(object): pass
site.register(Row)
Application()
"""
    class_bases = {
        "Row": ["Model"],
        "Rows": ["Manager"],
        "Query": ["QuerySet"],
        "Api": ["ModelSerializer"],
        "View": ["CustomAPIView"],
    }

    assert _findings(source, {"django"}, class_bases=class_bases) == Counter(
        {
            (1, "orm.django.model"): 1,
            (2, "orm.django.column"): 1,
            (3, "orm.django.fk"): 1,
            (4, "orm.django.m2m"): 1,
            (5, "orm.django.column_unmapped"): 1,
            (8, "foreign.django.manager"): 1,
            (9, "foreign.django.manager"): 1,
            (10, "foreign.django.drf"): 1,
            (11, "foreign.django.drf"): 1,
            (13, "foreign.django.admin"): 1,
        }
    )


def test_django_atomic_requires_an_imported_context_manager_call() -> None:
    source = """\
from django.db import transaction
with transaction.atomic():
    write()
transaction.atomic()
with stored:
    read()
"""

    assert _findings(source, {"django"}, with_imports=True) == Counter(
        {
            (2, "orm.transaction.atomic"): 1,
            (4, "foreign.django.api"): 1,
        }
    )


def test_monkeypatch_rules_cover_calls_and_import_origins() -> None:
    source = """\
import threading
import requests
import socket
patch_all()
monkey_patch()
spawn(work)
spawn_later(1, work)
Pool(4)
Group()
Timeout(1)
threading.local()
threading.Lock()
threading.RLock()
local()
requests.Session()
Session()
socket.create_connection(address)
"""

    assert _findings(source, {"gevent"}, with_imports=True) == Counter(
        {
            (4, "foreign.gevent.monkeypatch"): 1,
            (5, "foreign.gevent.monkeypatch"): 1,
            (6, "foreign.gevent.spawn"): 1,
            (7, "foreign.gevent.spawn"): 1,
            (8, "foreign.gevent.pool"): 1,
            (9, "foreign.gevent.pool"): 1,
            (10, "foreign.gevent.timeout"): 1,
            (11, "foreign.gevent.threading"): 1,
            (12, "foreign.gevent.threading"): 1,
            (13, "foreign.gevent.threading"): 1,
            (15, "foreign.gevent.blocking"): 1,
            (15, "foreign.gevent.session"): 1,
            (17, "foreign.gevent.blocking"): 1,
        }
    )


def test_same_rule_and_line_is_emitted_once() -> None:
    assert _findings("g.first; g.second\n", {"flask"}) == Counter({(1, "foreign.flask.proxy"): 1})


def test_no_framework_or_monkeypatch_has_no_findings() -> None:
    source = """\
Flask()
Bottle()
web.Response()
Application()
Configurator()
g.user
app["state"]
"""

    assert _findings(source, set()) == Counter()


def test_route_dialects_are_not_interchangeable() -> None:
    assert _findings(
        '@app.get("/items/<int:item>")\ndef handler(item: int): pass\n', {"flask"}
    ) == Counter({(1, "port.route.method"): 1})
    assert _findings(
        '@app.get("/items/<item:int>")\ndef handler(item: int): pass\n', {"bottle"}
    ) == Counter({(1, "port.route.method"): 1})
    assert _findings(
        '@app.get("/items/{item}")\ndef handler(item: str): pass\n', {"aiohttp"}
    ) == Counter({(1, "port.route.method"): 1})
    assert _findings(
        '@app.get("/items/<int:item>")\ndef handler(item: int): pass\n', {"aiohttp"}
    ) == Counter({(1, "foreign.aiohttp.route"): 1})


def test_decorator_rules_reject_wrong_names_and_frameworks() -> None:
    source = """\
@app.get("/x")
def route_lookalike(): pass
@app.middleware
def middleware_lookalike(): pass
@gen.coroutine
def coroutine_lookalike(): pass
@authenticated
def auth_lookalike(): pass
@custom
def custom(): pass
"""

    assert _findings(source, {"pyramid"}) == Counter()
    assert _findings(source, {"flask"}) == Counter({(1, "port.route.method"): 1})
    assert _findings(source, {"aiohttp"}) == Counter(
        {(1, "port.route.method"): 1, (3, "foreign.aiohttp.middleware"): 1}
    )


def test_called_tornado_decorator_and_imported_define_are_resolved_exactly() -> None:
    source = """\
from local import define
@gen.coroutine()
def old_style(): pass
options.other("x")
define("x")
"""

    assert _findings(source, {"tornado"}, with_imports=True) == Counter(
        {(2, "foreign.tornado.coroutine"): 1}
    )


def test_class_rules_reject_lookalikes() -> None:
    source = """\
class Handler(RequestHandler):
    __acl__ = []
    marker: int
    def __getitem__(self, key): return key
    def ordinary(self): pass
    def assign_attribute(self):
        self.__acl__ = []
class Plain(object):
    def method(self): return self.unknown
__acl__ = []
"""
    class_bases = {"Handler": ["RequestHandler"], "Plain": ["object"]}

    assert _findings(source, {"flask"}, class_bases=class_bases) == Counter()
    assert _findings(source, {"pyramid"}, class_bases=class_bases) == Counter(
        {
            (2, "foreign.pyramid.acl"): 1,
            (4, "foreign.pyramid.traversal"): 1,
            (10, "foreign.pyramid.acl"): 1,
        }
    )


def test_contract_dunders_are_inherited_framework_api() -> None:
    source = """\
class Handler(RequestHandler):
    def get(self):
        parent = self.__parent__
        return parent, self.__private
"""

    assert _findings(
        source,
        {"tornado"},
        class_bases={"Handler": ["RequestHandler"]},
        class_members={"Handler": {"get"}},
    ) == Counter(
        {
            (1, "foreign.tornado.handler"): 1,
            (2, "foreign.tornado.method"): 1,
            (3, "foreign.tornado.inherited"): 1,
        }
    )


def test_call_rules_reject_cross_framework_lookalikes() -> None:
    source = """\
Blueprint("router")
RouteTableDef()
register_blueprint(router)
app.router.remove("/x")
web.Other()
site.register(Row)
spawn(work)
Pool(2)
Timeout(1)
threading.Lock()
requests.Session()
abort(404)
redirect("/x")
"""

    assert _findings(source, {"bottle"}) == Counter(
        {
            (12, "port.http.exception"): 1,
            (13, "port.http.redirect"): 1,
        }
    )
    assert _findings(source, {"aiohttp"}) == Counter(
        {
            (2, "port.router.new"): 1,
            (3, "port.router.include"): 1,
            (12, "port.http.exception"): 1,
            (13, "port.http.redirect"): 1,
        }
    )
    assert _findings(source, {"flask"}) == Counter(
        {
            (1, "port.router.new"): 1,
            (3, "port.router.include"): 1,
            (12, "port.http.exception"): 1,
            (13, "port.http.redirect"): 1,
        }
    )


def test_monkeypatch_refuses_otherwise_translatable_constructs() -> None:
    source = """\
@app.get("/x")
def handler(): pass
RouteTableDef()
register_blueprint(router)
abort(404)
redirect("/x")
"""

    assert _findings(source, {"aiohttp", "gevent"}) == Counter({(1, "foreign.aiohttp.route"): 1})
    assert _findings(source, {"flask", "gevent"}) == Counter({(1, "foreign.flask.route"): 1})


def test_django_atomic_rejects_non_default_and_unresolved_calls() -> None:
    source = """\
from django.db import transaction
with transaction.atomic(using="replica"):
    write()
"""

    assert _findings(source, {"django"}, with_imports=True) == Counter(
        {(2, "foreign.django.api"): 1}
    )
    assert _findings("with transaction.atomic():\n    write()\n", {"django"}) == Counter()


def test_blocking_origins_require_a_monkeypatch() -> None:
    source = """\
import requests
requests.get("https://example.invalid")
"""

    assert _findings(source, {"flask"}, with_imports=True) == Counter()


def test_final_api_fallback_deduplicates_a_rule_on_one_line() -> None:
    source = """\
from aiohttp import web
web.first; web.second
"""

    assert _findings(source, {"aiohttp"}, with_imports=True) == Counter(
        {
            (2, "foreign.aiohttp.api"): 1,
        }
    )


def test_tornado_family_names_do_not_apply_to_other_frameworks() -> None:
    source = """\
class Socket(WebSocketHandler):
    def open(self): pass
"""

    assert (
        _findings(
            source,
            {"flask"},
            class_bases={"Socket": ["WebSocketHandler"]},
        )
        == Counter()
    )


def test_django_inherited_api_requires_family_membership() -> None:
    source = """\
class Plain(object):
    def method(self): return self.unknown
"""

    assert (
        _findings(
            source,
            {"django"},
            class_bases={"Plain": ["object"]},
            class_members={"Plain": {"method"}},
        )
        == Counter()
    )


def test_pyramid_acl_targets_must_be_names() -> None:
    source = """\
class Resource:
    owner.__acl__ = []
"""

    assert _findings(source, {"pyramid"}) == Counter()


def test_monkeypatch_call_names_are_exact() -> None:
    source = """\
import requests
patch_all()
threading.other()
requests.get("https://example.invalid")
abort(404)
redirect("/x")
"""

    assert _findings(source, {"gevent"}, with_imports=True) == Counter(
        {
            (2, "foreign.gevent.monkeypatch"): 1,
            (4, "foreign.gevent.blocking"): 1,
        }
    )
    assert _findings("patch_all()\n", {"flask"}) == Counter()


def test_tornado_options_and_django_atomic_are_framework_gated() -> None:
    tornado_lookalike = "options.define('x')\n"
    django_lookalike = """\
from django.db import transaction
with transaction.atomic():
    write()
"""

    assert _findings(tornado_lookalike, {"flask"}) == Counter()
    assert _findings(django_lookalike, {"flask"}, with_imports=True) == Counter()


def test_framework_parameter_conventions_do_not_leak_to_flask() -> None:
    source = """\
def handler(request, context, config):
    return request.value, context.value, config.value
"""

    assert _findings(source, {"flask"}) == Counter()
