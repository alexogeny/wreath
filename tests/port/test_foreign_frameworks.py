from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")

from wreath._port.detect import Detection, scan_module  # noqa: E402  (after importorskip)


def _detect(source: str, name: str = "app.py") -> Detection:
    import ast

    return Detection.of({name: scan_module(ast.parse(source))})


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from flask import Flask\napp = Flask(__name__)\n", "Flask"),
        ("from django.db import models\n", "Django"),
        ("import tornado.web\n", "Tornado"),
        ("from pyramid.config import Configurator\n", "Pyramid"),
        ("from aiohttp import web\n", "aiohttp"),
        ("from bottle import Bottle\n", "Bottle"),
        ("from fastapi import FastAPI\n", "FastAPI"),
    ],
)
def test_the_framework_is_named_from_imports_alone(source: str, expected: str) -> None:
    assert expected in _detect(source).headline()


def test_a_relative_import_names_no_framework() -> None:
    detection = _detect("from .models import Thing\nfrom ..util import helper\n")
    assert detection.frameworks == {}
    assert "no web framework recognized" in detection.headline()


def test_a_hook_name_is_not_a_flask_hook_without_flask(tmp_path) -> None:
    source = tmp_path / "plain.py"
    source.write_text(
        "from aiohttp import web\n"
        "app = web.Application()\n"
        "@app.before_request\n"
        "def prepare():\n"
        "    pass\n",
        encoding="utf-8",
    )

    assert "foreign.flask.hook" not in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


def test_a_flask_before_request_decorator_is_reported_as_a_hook(tmp_path) -> None:
    source = tmp_path / "flask_app.py"
    source.write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.before_request\n"
        "def prepare():\n"
        "    pass\n",
        encoding="utf-8",
    )

    assert "foreign.flask.hook" in [finding.rule_id for finding in port.analyze(source).findings]


def test_an_unrecognised_flask_decorator_is_not_reported_as_a_hook(tmp_path) -> None:
    source = tmp_path / "flask_app.py"
    source.write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.custom_decorator\n"
        "def prepare():\n"
        "    pass\n",
        encoding="utf-8",
    )

    assert "foreign.flask.hook" not in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


def test_a_tree_with_no_framework_says_so_rather_than_nothing() -> None:
    detection = _detect("import json\n\n\ndef total(rows):\n    return sum(rows)\n")
    assert not detection.portable
    assert any("No web framework recognized" in w for w in detection.warnings())


def test_monkeypatching_is_a_refusal_not_a_low_score() -> None:
    detection = _detect(
        "from gevent import monkey\n"
        "monkey.patch_all()\n"
        "\n"
        "from bottle import Bottle\n"
        "app = Bottle()\n"
    )
    assert detection.monkeypatched
    assert detection.patch_sites == (("app.py", 2),)
    assert not detection.portable
    assert any("monkeypatched" in w for w in detection.warnings())


def test_monkeypatching_disqualifies_even_a_fastapi_tree() -> None:
    detection = _detect(
        "from gevent import monkey\n"
        "monkey.patch_all()\n"
        "\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
    )
    assert detection.target_modules == 1
    assert not detection.portable


def test_patch_all_without_a_runtime_import_is_not_monkeypatching() -> None:
    detection = _detect("from mylib import patch_all\npatch_all()\n")
    assert not detection.monkeypatched


BOTTLE_APP = """\
from bottle import Bottle

app = Bottle()


@app.get("/rates")
def list_rates():
    return {}


@app.post("/rates")
def push_rates():
    return {}
"""


def test_a_bottle_route_is_not_a_fastapi_route(tmp_path) -> None:
    (tmp_path / "app.py").write_text(BOTTLE_APP)
    report = port.analyze(tmp_path)

    assert [f for f in report.findings if f.category == "routing"] == []
    assert not any(f.rule_id.startswith("route.") for f in report.findings)
    # The routing category stays inapplicable, which is the property that
    # failed. Overall coverage is no longer zero and should not be: these
    # decorators *are* translated now, as `port.route.method` in the `foreign`
    # category — recognized as Bottle and ported as Bottle. What must never
    # happen is them being counted as the FastAPI rules, and the two assertions
    # above are that, stated directly.
    assert report.coverage("routing") is None
    assert {f.rule_id for f in report.findings} >= {"port.route.method"}
    assert report.coverage("foreign") is not None


def test_a_fastapi_route_still_counts(tmp_path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "\n"
        '@app.get("/rates")\n'
        "async def list_rates():\n"
        "    return {}\n"
    )
    report = port.analyze(tmp_path)

    assert [f for f in report.findings if f.rule_id == "route.method"]
    assert report.coverage_overall() == 1.0


def test_a_router_imported_from_elsewhere_still_counts(tmp_path) -> None:
    (tmp_path / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "from .shared import router\n"
        "\n"
        "\n"
        '@router.get("/rates")\n'
        "async def list_rates():\n"
        "    return {}\n"
    )
    report = port.analyze(tmp_path)

    assert [f for f in report.findings if f.rule_id == "route.method"]


@pytest.mark.parametrize(
    ("source", "why"),
    [
        (
            'import tornado.options\ntornado.options.define("rotation_years", default=15)\n',
            "the fully qualified spelling",
        ),
        (
            'from tornado import options\noptions.define("rotation_years", default=15)\n',
            "the module imported and dotted through",
        ),
        (
            'from tornado.options import define\ndefine("rotation_years", default=15)\n',
            "the canonical spelling, which the dotted text does not contain",
        ),
    ],
)
def test_every_spelling_of_a_tornado_option_is_the_options_rule(
    tmp_path, source: str, why: str
) -> None:
    (tmp_path / "app.py").write_text(source)
    rules = {f.rule_id for f in port.analyze(tmp_path).findings}
    assert "foreign.tornado.options" in rules, why


def test_a_class_attribute_is_not_a_name_any_later_scope_can_see(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from django.db import models\n"
        "\n"
        "\n"
        "class Strandline(models.Model):\n"
        "    beach = models.CharField(max_length=64)\n"
        "\n"
        "\n"
        "def normalize(row):\n"
        "    beach = row.beach.strip().lower()\n"
        "    return beach\n"
    )
    findings = port.analyze(tmp_path).findings

    assert [f.line for f in findings if f.rule_id == "foreign.django.api"] == []
    # The field itself is still read as a column: nothing was suppressed, the
    # binding was simply attributed to the scope that made it.
    assert [f.rule_id for f in findings if f.line == 5] == ["orm.django.column"]


def test_a_django_query_is_refused_when_the_model_carries_a_manager(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from django.db import models\n"
        "\n"
        "\n"
        "class ClearedManager(models.Manager):\n"
        "    def get_queryset(self):\n"
        "        return super().get_queryset().exclude(cleared_at=None)\n"
        "\n"
        "\n"
        "class Planting(models.Model):\n"
        "    parcel = models.CharField(max_length=32)\n"
        "    objects = ClearedManager()\n"
        "\n"
        "\n"
        "def by_parcel(parcel):\n"
        "    return Planting.objects.filter(parcel=parcel)\n"
    )
    findings = port.analyze(tmp_path).findings

    assert [f for f in findings if f.category == "queries"] == []
    (query,) = [f for f in findings if f.line == 15]
    assert query.rule_id == "foreign.django.query"
    assert query.tag == port.UNSUPPORTED
    assert "get_queryset" in query.message


def test_a_manager_free_django_model_queries_like_any_other_model(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from django.db import models\n"
        "\n"
        "\n"
        "class Planting(models.Model):\n"
        "    parcel = models.CharField(max_length=32)\n"
    )
    (tmp_path / "reads.py").write_text(
        "from django.db import transaction\n"
        "\n"
        "from .m import Planting\n"
        "\n"
        "\n"
        "def by_parcel(parcel):\n"
        "    with transaction.atomic():\n"
        "        return Planting.objects.filter(parcel=parcel)\n"
    )
    (query,) = [f for f in port.analyze(tmp_path).findings if f.construct == "orm_query"]

    assert query.rule_id == "orm.query.filter_exact"
    assert query.tag == port.TRANSLATED


def test_a_manager_on_an_ancestor_still_refuses_the_subclass(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from django.db import models\n"
        "\n"
        "\n"
        "class LiveManager(models.Manager):\n"
        "    def get_queryset(self):\n"
        "        return super().get_queryset().filter(live=True)\n"
        "\n"
        "\n"
        "class Base(models.Model):\n"
        "    objects = LiveManager()\n"
        "\n"
        "\n"
        "class Planting(Base):\n"
        "    parcel = models.CharField(max_length=32)\n"
        "\n"
        "\n"
        "def by_parcel(parcel):\n"
        "    return Planting.objects.filter(parcel=parcel)\n"
    )
    findings = port.analyze(tmp_path).findings

    assert [f for f in findings if f.category == "queries"] == []
    assert [f.rule_id for f in findings if f.line == 18] == ["foreign.django.query"]


def test_a_save_override_refuses_the_models_queries_as_well(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from django.db import models\n"
        "\n"
        "\n"
        "class Planting(models.Model):\n"
        "    parcel = models.CharField(max_length=32)\n"
        "\n"
        "    def save(self, *args, **kwargs):\n"
        "        self.parcel = self.parcel.strip()\n"
        "        super().save(*args, **kwargs)\n"
        "\n"
        "\n"
        "def by_parcel(parcel):\n"
        "    return Planting.objects.filter(parcel=parcel)\n"
    )
    assert [f.rule_id for f in port.analyze(tmp_path).findings if f.line == 13] == [
        "foreign.django.query"
    ]


def test_a_model_the_tree_never_declares_stays_refused_in_a_django_module(
    tmp_path,
) -> None:
    (tmp_path / "m.py").write_text(
        "from django.contrib.auth.models import User\n"
        "\n"
        "\n"
        "def by_email(email):\n"
        "    return User.objects.filter(email=email)\n"
    )
    findings = port.analyze(tmp_path).findings

    assert "foreign.django.query" in {f.rule_id for f in findings if f.line == 5}
    assert [f for f in findings if f.category == "queries"] == []


DJANGO_MODEL_HEAD = (
    "from django.db import models\n"
    "\n"
    "\n"
    "class ClearedManager(models.Manager):\n"
    "    def get_queryset(self):\n"
    "        return super().get_queryset().exclude(cleared_at=None)\n"
    "\n"
    "\n"
    "class Strandline(models.Model):\n"
)


def test_a_django_model_that_is_only_fields_is_a_class_header_rename(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        DJANGO_MODEL_HEAD + "    beach = models.CharField(max_length=64)\n"
    )
    (model,) = [f for f in port.analyze(tmp_path).findings if f.construct == "orm_model"]

    assert model.rule_id == "orm.django.model"
    assert model.tag == port.TRANSLATED


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (
            "    cleared = ClearedManager()\n    beach = models.CharField(max_length=64)\n",
            "a second manager carries a predicate the class header does not",
        ),
        (
            "    beach = models.CharField(max_length=64)\n"
            "\n"
            "    def save(self, *args, **kwargs):\n"
            "        self.beach = self.beach.strip()\n"
            "        super().save(*args, **kwargs)\n",
            "a save() override runs on every write and has no declarative form",
        ),
    ],
)
def test_a_django_model_with_behaviour_is_not_a_class_header_rename(
    tmp_path, body: str, why: str
) -> None:
    (tmp_path / "m.py").write_text(DJANGO_MODEL_HEAD + body)
    findings = port.analyze(tmp_path).findings
    (model,) = [f for f in findings if f.construct == "django_model"]

    assert model.rule_id == "foreign.django.model", why
    assert model.tag == port.UNSUPPORTED
    assert "orm.django.model" not in {f.rule_id for f in findings}
    # The fields are still read one at a time: the class is refused, not the
    # column mapping underneath it.
    assert "orm.django.column" in {f.rule_id for f in findings}


def test_an_ormar_query_of_the_same_shape_still_translates(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "import ormar\n"
        "\n"
        "\n"
        "class Planting(ormar.Model):\n"
        "    parcel: str = ormar.String(max_length=32)\n"
        "\n"
        "\n"
        "async def by_parcel(parcel):\n"
        "    return await Planting.objects.filter(parcel=parcel).all()\n"
    )
    (query,) = [f for f in port.analyze(tmp_path).findings if f.category == "queries"]

    assert query.rule_id == "orm.query.filter_exact"
    assert query.tag == port.TRANSLATED


def test_a_module_level_alias_still_resolves_everywhere(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from aiohttp import web\n"
        "\n"
        "Reply = web.Response\n"
        "\n"
        "\n"
        "async def handler(unused):\n"
        "    return Reply(status=204)\n"
    )
    findings = port.analyze(tmp_path).findings

    assert [f.rule_id for f in findings if f.line == 7] == ["foreign.aiohttp.api"]


def test_a_local_alias_resolves_inside_the_function_that_binds_it(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from aiohttp import web\n"
        "\n"
        "\n"
        "def build():\n"
        "    application = web.Application()\n"
        "    return application\n"
        "\n"
        "\n"
        "def unrelated():\n"
        "    application = object()\n"
        "    return application\n"
    )
    lines = [f.line for f in port.analyze(tmp_path).findings if f.rule_id == "foreign.aiohttp.api"]

    assert 6 in lines
    assert 10 not in lines and 11 not in lines


def test_the_report_json_leads_with_the_detected_stack(tmp_path) -> None:
    (tmp_path / "app.py").write_text(BOTTLE_APP)
    payload = port.analyze(tmp_path).as_dict()

    assert payload["detection"]["frameworks"] == {"Bottle": 1}
    assert payload["detection"]["portable"] is False
    assert payload["detection"]["warnings"]


def test_the_markdown_says_the_stack_before_the_numbers(tmp_path) -> None:
    (tmp_path / "app.py").write_text(BOTTLE_APP)
    markdown = port.analyze(tmp_path).to_markdown()

    assert "stack detected" in markdown
    assert markdown.index("stack detected") < markdown.index("files analyzed")
    assert "translates FastAPI and Starlette" in markdown


EXPECTED = {
    "cairn_index": "Pyramid",
    "coppice_console": "Tornado",
    "estuary_hub": "aiohttp",
    "fernbrake_portal": "Pyramid",
    "heathland_sync": "Bottle",
    "hollowbeck_survey": "Flask",
    "ironwood_tally": "Django",
    "juniper_depot": "aiohttp",
    "larkspur_roster": "Tornado",
    "mosswood_almanac": "Pyramid",
    "mudflat_gauge": "Flask",
    "saltpan_monitor": "Tornado",
    "sandbar_kiosk": "Bottle",
    "scrubland_permits": "Flask",
    "thicket_registry": "Django",
    "tidepool_feed": "aiohttp",
    "wracklines_registry": "Django",
}

#: The fixture that is the *authority* for a rule family -> every rule id it is
#: expected to produce, exactly. Equality rather than containment on purpose: a
#: missing id is a rule that has stopped firing, and an extra one is a finding
#: attributed to a construct that is not there -- which is how a class attribute
#: taught the analyzer that every later local of the same name was a framework
#: object. Both directions have been wrong, so both are pinned.
FAMILIES = {
    # Tornado's `HTTPError(404)` is the one construct in this tree with an exact
    # wreath spelling. The handler classes around it are not: what a route does
    # is spread across the base-class chain, and none of it is written in the
    # class that declares it.
    "saltpan_monitor": {
        "foreign.tornado.api",
        "foreign.tornado.coroutine",
        "foreign.tornado.handler",
        "foreign.tornado.inherited",
        "foreign.tornado.method",
        "foreign.tornado.routes",
        "port.http.exception",
    },
    "coppice_console": {
        "foreign.tornado.api",
        "foreign.tornado.authenticated",
        "foreign.tornado.coroutine",
        "foreign.tornado.handler",
        "foreign.tornado.hook",
        "foreign.tornado.inherited",
        "foreign.tornado.method",
        "foreign.tornado.options",
        "foreign.tornado.periodic",
        "foreign.tornado.routes",
        "foreign.tornado.websocket",
    },
    "fernbrake_portal": {
        "foreign.pyramid.acl",
        "foreign.pyramid.api",
        "foreign.pyramid.config",
        "foreign.pyramid.include",
        "foreign.pyramid.registry",
        "foreign.pyramid.request",
        "foreign.pyramid.route",
        "foreign.pyramid.traversal",
        "foreign.pyramid.tween",
        "foreign.pyramid.view",
    },
    "sandbar_kiosk": {
        "foreign.bottle.api",
        "foreign.bottle.app",
        "foreign.bottle.route",
        "foreign.gevent.api",
        "foreign.gevent.blocking",
        "foreign.gevent.monkeypatch",
        "foreign.gevent.pool",
        "foreign.gevent.session",
        "foreign.gevent.spawn",
        "foreign.gevent.threading",
        "foreign.gevent.timeout",
    },
    # The mixed fixture, and the reason mixed fixtures are the point. The
    # `abort(404)` in `show_permit` has an exact wreath spelling and is written
    # out; the `before_request` two functions above it, and the `g.commons` that
    # handler *reads*, do not and are not. A rule that widened from the first
    # onto the second would show up here as a missing `foreign.flask.hook`.
    "scrubland_permits": {
        "foreign.flask.api",
        "foreign.flask.hook",
        "foreign.flask.proxy",
        "port.app.wreath",
        "port.http.exception",
        "port.route.method",
        "port.router.include",
        "port.router.new",
    },
    "tidepool_feed": {
        "foreign.aiohttp.api",
        "foreign.aiohttp.app",
        "foreign.aiohttp.client",
        "foreign.aiohttp.lifecycle",
        "foreign.aiohttp.middleware",
        "foreign.aiohttp.request",
        "foreign.aiohttp.response",
        "foreign.aiohttp.route",
        "foreign.aiohttp.route_dynamic",
        "foreign.aiohttp.state",
        "foreign.aiohttp.subapp",
        # `RouteTableDef()` is a `Router()` and `add_routes(x)` is
        # `include_router(x)`; the handlers hanging off them are not, because an
        # aiohttp one reads `match_info` and the app dict in its body rather
        # than declaring anything in its signature.
        "port.router.include",
        "port.router.new",
    },
    # The negative control for the tree-wide manager gate. `Sighting` declares
    # `objects = ActiveManager()` and overrides `save()`, so every chain against
    # it stays `foreign.django.query` however plainly its verbs are spelled --
    # and `orm.query.filter_exact`, the verdict `corpus/birchmoor_tally` earns
    # for exactly those verbs, appears nowhere here.
    "ironwood_tally": {
        "foreign.django.admin",
        "foreign.django.api",
        "foreign.django.drf",
        "foreign.django.inherited",
        "foreign.django.manager",
        "foreign.django.model",
        "foreign.django.query",
        "orm.django.column",
        "orm.django.column_unmapped",
        "orm.django.m2m",
        "orm.django.model",
        # `Observer` is manager-free, so the gate lets its one chain through --
        # and the chain itself is refused, because `sightings` is the reverse of
        # a ManyToManyField and wreath has no model for the table Django made
        # implicitly. Needs-review with the relation named, never a rewrite.
        "orm.query.filter",
    },
    "wracklines_registry": {
        "foreign.django.admin",
        "foreign.django.api",
        "foreign.django.drf",
        "foreign.django.inherited",
        "foreign.django.manager",
        "foreign.django.model",
        "orm.django.column",
        "orm.django.column_unmapped",
        "orm.django.fk",
        "orm.django.m2m",
        "orm.django.model",
    },
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_fixture_covers_its_rule_family_without_being_misidentified(
    foreign_root, name: str
) -> None:
    report = port.analyze(foreign_root / name)
    if name in FAMILIES:
        assert {finding.rule_id for finding in report.findings} == FAMILIES[name]
    assert EXPECTED[name] in report.detection.headline(), name
    assert not report.detection.portable, name
    assert report.detection.warnings(), name
    routing = [finding for finding in report.findings if finding.category == "routing"]
    assert routing == [], f"{name}: {routing}"


def test_every_foreign_fixture_is_named_correctly(foreign_app_roots) -> None:
    assert [p.name for p in foreign_app_roots] == sorted(EXPECTED)


@pytest.mark.parametrize(
    ("source", "framework"),
    [
        (BOTTLE_APP, "Bottle"),
        (
            "from flask import Flask\n"
            "\n"
            "app = Flask(__name__)\n"
            "\n"
            "\n"
            '@app.route("/rates")\n'
            "def list_rates():\n"
            "    return {}\n",
            "Flask",
        ),
        (
            "from aiohttp import web\n"
            "\n"
            "routes = web.RouteTableDef()\n"
            "\n"
            "\n"
            '@routes.get("/rates")\n'
            "async def list_rates(request):\n"
            "    return web.json_response({})\n",
            "aiohttp",
        ),
    ],
)
def test_a_foreign_route_decorator_is_never_a_fastapi_route_finding(
    tmp_path, source: str, framework: str
) -> None:
    (tmp_path / "app.py").write_text(source)
    report = port.analyze(tmp_path)
    findings = report.findings

    assert findings, f"{framework}: the decorators must still be counted"
    assert [f for f in findings if f.category == "routing"] == []
    assert not any(f.rule_id.startswith("route.") for f in findings)
    # `None`, never 1.0: a category that recognized nothing is inapplicable, and
    # reporting it as perfect is the shape of the original bug.
    assert report.coverage("routing") is None


def test_the_monkeypatched_fixture_is_refused_by_name(foreign_root) -> None:
    detection = port.analyze(foreign_root / "heathland_sync").detection
    assert detection.monkeypatched
    assert [site[0] for site in detection.patch_sites] == ["sync.py"]


def test_the_django_fixture_is_not_reported_as_ormar(foreign_root) -> None:
    detection = port.analyze(foreign_root / "thicket_registry").detection
    # Keyed by root package here; `as_dict` is where display names appear.
    assert detection.frameworks == {"django": 1}
    assert "Django" in detection.headline()
    assert not detection.portable
