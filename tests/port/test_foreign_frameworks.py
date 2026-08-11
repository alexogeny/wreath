"""Trees `wreath port` cannot port, and what it owes the reader about them.

The tool translates FastAPI and Starlette. Run it at anything else and there are
only two honest answers -- "this is Django, which I do not port" or "this is
monkeypatched, which no static rewrite survives". It used to give neither.

Two failures motivate every test here, both found by running the tool over a set
of applications written in frameworks it does not target:

* **Silence with a zero exit.** aiohttp, Tornado and Pyramid trees produced no
  findings at all, `coverage_overall: null`, and exit 0. Nothing separates that
  from a clean bill of health, and one of those trees has eighty-three runtime
  URLs the analyzer cannot see.
* **A perfect score off a spelling coincidence.** A Bottle application scored
  1.00 coverage, because `@app.get("/x")` is spelled the same in Bottle as in
  FastAPI and the route rule never checked which one it was looking at. The one
  tree in the set that must be refused outright -- it is monkeypatched -- was the
  one the tool was most confident about.

The fixtures live in ``foreign/`` rather than ``corpus/`` on purpose, and what
that division means has narrowed. It used to be "everything here yields no
findings", which was true only while a foreign framework was entirely
unportable. Flask, aiohttp, Tornado, Pyramid and Bottle constructs with an exact
wreath spelling are translated now, so a ``foreign/`` root producing
``translated`` findings is the tool doing its job.

What survives is the pair of properties the second failure above actually
describes:

* ``detection.portable`` is ``False`` and the warnings fire, for every root
  here. A foreign or monkeypatched tree is never reported as portable.
* **No finding lands in the ``routing`` category.** That is precisely what went
  wrong: ``route.method`` — the *FastAPI* route rule — fired on Bottle because
  the decorator is spelled the same, and the headline number was computed from
  constructs the tool had misidentified. Every foreign-framework finding stays
  in the ``foreign`` category whatever its verdict, so this is structural.

Mixed fixtures are the point rather than an untidiness. ``scrubland_permits``
carries a translatable ``@app.route("/healthz")`` and an untranslatable
``before_request``/``g`` in the same file, and pinning both is what stops a rule
widening from the first onto the second.
"""
from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")

from wreath._port.detect import Detection, scan_module  # noqa: E402  (after importorskip)


def _detect(source: str, name: str = "app.py") -> Detection:
    import ast

    return Detection.of({name: scan_module(ast.parse(source))})


# -- naming the stack --------------------------------------------------------

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
    """No execution, no installed packages -- the import statement is enough."""
    assert expected in _detect(source).headline()


def test_a_relative_import_names_no_framework() -> None:
    """`from .models import Grant` says nothing about the stack, and must not.

    A relative import has no root package of its own; counting its first segment
    would invent a framework named after the local module.
    """
    detection = _detect("from .models import Thing\nfrom ..util import helper\n")
    assert detection.frameworks == {}
    assert "no web framework recognized" in detection.headline()


def test_a_tree_with_no_framework_says_so_rather_than_nothing() -> None:
    detection = _detect("import json\n\n\ndef total(rows):\n    return sum(rows)\n")
    assert not detection.portable
    assert any("No web framework recognized" in w for w in detection.warnings())


# -- refusing what cannot survive a rewrite ----------------------------------

def test_monkeypatching_is_a_refusal_not_a_low_score() -> None:
    """`monkey.patch_all()` reinterprets every blocking call beneath it.

    A rewrite to `async def` over code that relies on implicit yield points
    passes its tests at low concurrency and serialises in production, so the
    verdict is "cannot port", not "ported with caveats".
    """
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
    """The framework is not what breaks; the calls underneath it are."""
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
    """Something else owns the name `patch_all` -- a test helper, most often."""
    detection = _detect("from mylib import patch_all\npatch_all()\n")
    assert not detection.monkeypatched


# -- not claiming coverage on a coincidence ----------------------------------

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
    """The decorator is spelled identically. The framework is not the same one.

    Before the gate, this scored 1.00 coverage: every `@app.get` counted as a
    translated route, so the report's headline number was computed entirely from
    constructs the tool had misidentified.
    """
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
    """The control for the gate above: same decorator shape, right framework."""
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
    """The gate is per-module and asks only whether the framework is imported.

    A stricter gate -- "the decorated name must resolve to an APIRouter in this
    module" -- would drop the very common case of a router built in one module
    and decorated in another, turning a false positive into a false negative on
    real FastAPI code. Importing fastapi at all is the cheap signal that keeps
    this case while still refusing Bottle.
    """
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


# -- resolving a name rather than reading its spelling ------------------------

@pytest.mark.parametrize(
    ("source", "why"),
    [
        (
            "import tornado.options\n"
            'tornado.options.define("rotation_years", default=15)\n',
            "the fully qualified spelling",
        ),
        (
            "from tornado import options\n"
            'options.define("rotation_years", default=15)\n',
            "the module imported and dotted through",
        ),
        (
            "from tornado.options import define\n"
            'define("rotation_years", default=15)\n',
            "the canonical spelling, which the dotted text does not contain",
        ),
    ],
)
def test_every_spelling_of_a_tornado_option_is_the_options_rule(
    tmp_path, source: str, why: str
) -> None:
    """`define(...)` was matched by looking for "options" in the call's *text*.

    The bare import is the spelling Tornado's own documentation uses, and it
    leaves nothing but `define` at the call site -- so a process-wide global
    configuration surface was billed as an unremarkable piece of the Tornado
    API. The import table already knows where the name came from.
    """
    (tmp_path / "app.py").write_text(source)
    rules = {f.rule_id for f in port.analyze(tmp_path).findings}
    assert "foreign.tornado.options" in rules, why


def test_a_class_attribute_is_not_a_name_any_later_scope_can_see(tmp_path) -> None:
    """`beach = models.CharField(...)` binds nothing outside its class body.

    Assignment aliases exist because `Response = web.Response` is invisible to
    the import table. Collecting them with a whole-module walk collected class
    attributes too, so a Django model with a field called `beach` made every
    later local named `beach` resolve to `django.db.models.CharField` -- and
    each one billed as unportable Django API. The finding was on a line that
    touches no framework at all.
    """
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
    """`.objects.filter()` is spelled the same in both, and means different things.

    ormar's `objects` is every row; Django's is whatever `get_queryset()` left,
    and that predicate appears at no call site. So the ormar verdict here would
    be `translated` with a rewrite attached -- `Model.select().where(...)` --
    which silently widens the query for exactly the rows somebody meant to hide.

    The discriminator is the *model*, not the querying module. `cleared` is a
    `ClearedManager`, so nothing about `Planting.objects` is every row.
    """
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
    """The control, and the reason the gate had to move.

    Nothing here declares a manager, overrides `save`, or subclasses `Manager`,
    so `Planting.objects` *is* every row in `planting` -- which is exactly what
    `Planting.select()` is. Gating this on whether the querying module happens to
    import Django answered a different question: two modules carrying identical
    chains against these same models got opposite verdicts because one of them
    also said `from django.db import transaction`.
    """
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
    (query,) = [
        f for f in port.analyze(tmp_path).findings if f.construct == "orm_query"
    ]

    assert query.rule_id == "orm.query.filter_exact"
    assert query.tag == port.TRANSLATED


def test_a_manager_on_an_ancestor_still_refuses_the_subclass(tmp_path) -> None:
    """Django inherits managers, so reading one class body is not enough.

    An abstract base carrying `objects = LiveManager()` hands its predicate to
    every model that inherits it, and none of those class bodies mentions it.
    """
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
    """`save()` is on the write path, and a write chain runs through `objects`."""
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
    assert [
        f.rule_id for f in port.analyze(tmp_path).findings if f.line == 13
    ] == ["foreign.django.query"]


def test_a_model_the_tree_never_declares_stays_refused_in_a_django_module(
    tmp_path,
) -> None:
    """`django.contrib.auth`'s User is not in the source, so its manager is not either.

    The fallback matters as much as the rule: a name the tree-wide walk never
    saw declared has no readable manager, and a Django-importing module is the
    only evidence available. Guessing "plain" there would translate every query
    against every model in every third-party app.
    """
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
    """The control for the split below: nothing but columns, so nothing is lost."""
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
    """`class X(models.Model)` was always the translated rule, so its own rule was dead.

    `foreign.django.model` says what does not carry: the fields move, and the
    manager's predicate and anything in `save()` do not. Every model in the
    corpus was claimed by the rename first, which is why the rule describing
    the hard half could not fire anywhere.
    """
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
    """The control for the gate: same verb, same arguments, no Django import."""
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
    """The control: `Response = web.Response` is why aliases exist at all."""
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
    """An app built in a factory is the aiohttp idiom, and it is still an alias.

    Scoping is not the same as ignoring locals: the name is live for the rest of
    the function that bound it, and nested functions see it too.
    """
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


# -- the report carries the verdict ------------------------------------------

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


# -- the fixture tree ---------------------------------------------------------

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


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_a_fixture_covers_its_whole_rule_family_and_nothing_else(
    foreign_root, name: str
) -> None:
    assert {f.rule_id for f in port.analyze(foreign_root / name).findings} == FAMILIES[name]


def test_every_foreign_fixture_is_named_correctly(foreign_app_roots) -> None:
    assert [p.name for p in foreign_app_roots] == sorted(EXPECTED)
    for root in foreign_app_roots:
        detection = port.analyze(root).detection
        assert EXPECTED[root.name] in detection.headline(), root.name


def test_no_foreign_fixture_is_reported_as_portable(foreign_app_roots) -> None:
    """None of these is a tree `wreath port` can translate, and it must say so.

    The failure this pins is not a low score — it is a *confident* one. Scoring
    any of these above zero means a rule matched a spelling rather than a
    framework.
    """
    for root in foreign_app_roots:
        report = port.analyze(root)
        assert not report.detection.portable, root.name
        assert report.detection.warnings(), root.name


def test_no_foreign_fixture_yields_a_routing_finding(foreign_app_roots) -> None:
    """The surviving half of the old blanket contract, and the load-bearing half.

    `foreign/` roots may yield translated findings now — Flask's `@app.route`
    has a wreath spelling and the emitter writes it. What they must never yield
    is a **routing** finding, because that category is the *FastAPI* route rules
    and one firing here means the framework was misidentified rather than
    translated.
    """
    for root in foreign_app_roots:
        routing = [f for f in port.analyze(root).findings if f.category == "routing"]
        assert routing == [], f"{root.name}: {routing}"


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
    """The Bottle-1.00 regression, stated directly instead of as a side effect.

    `@app.get("/x")` and `@routes.get("/x")` are spelled exactly as FastAPI
    spells them, and the route rules used to fire on all three — which is how a
    monkeypatched Bottle application came back as 100% auto-translatable off
    nineteen decorators the tool had never seen the framework of.

    Translating these is now allowed and expected; being *counted as FastAPI* is
    not. So the assertion is about which rule fired, not about whether one did:
    the finding belongs to `foreign`, the routing category recognizes nothing at
    all here, and `coverage("routing")` is `None` rather than a perfect score.
    """
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
    """`.objects.filter()` is spelled the same; the manager underneath is not.

    Django's default manager here filters soft-deleted rows out, so a query
    rewritten on ormar's terms silently widens. Naming the framework is what
    stops the advice being given in the first place.
    """
    detection = port.analyze(foreign_root / "thicket_registry").detection
    # Keyed by root package here; `as_dict` is where display names appear.
    assert detection.frameworks == {"django": 1}
    assert "Django" in detection.headline()
    assert not detection.portable
