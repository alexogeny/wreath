"""Foreign-framework constructs that do carry across, and the ones that do not.

Five frameworks say the same things five ways. `abort(404)`,
`web.HTTPNotFound()`, `HTTPError(404)`, `HTTPNotFound()` and `Http404()` are all
`raise NotFound()`, so the table that decides it lives in `_port/frameworks.py`
once rather than five times -- the fifth spelling is always the one nobody
remembers to add.

Every test here comes in a pair. A construct is translated only where the target
is exact, and the second half of each pair is the near-miss that has to keep
failing loudly: a 418 with no wreath class, a `uuid` converter wreath's binder
cannot convert, a regex converter that would widen the route, a monkeypatched
tree where a rewrite that *looks* like it worked is worse than none.
"""
from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")


def _analyze(tmp_path, source: str, name: str = "app.py"):
    (tmp_path / name).write_text(source)
    return port.analyze(tmp_path).findings


def _emit(tmp_path, source: str, name: str = "app.py") -> str:
    (tmp_path / name).write_text(source)
    out = tmp_path / "out"
    port.port_tree(tmp_path, out)
    return (out / name).read_text(encoding="utf-8")


def _rules(findings) -> set[str]:
    return {f.rule_id for f in findings}


# -- HTTP errors ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("imports", "call", "expected"),
    [
        ("from flask import Flask, abort", "abort(404)", "raise NotFound()"),
        (
            "from flask import Flask, abort",
            'abort(409, description="clash")',
            'raise Conflict("clash")',
        ),
        ("from bottle import Bottle, abort", 'abort(409, "clash")', 'raise Conflict("clash")'),
        (
            "from aiohttp import web",
            'raise web.HTTPNotFound(reason="gone")',
            'raise NotFound("gone")',
        ),
        ("from aiohttp import web", "raise web.HTTPConflict()", "raise Conflict()"),
        (
            "from pyramid.httpexceptions import HTTPNotFound",
            'raise HTTPNotFound("gone")',
            'raise NotFound("gone")',
        ),
        ("import tornado.web", "raise tornado.web.HTTPError(404)", "raise NotFound()"),
        ("from django.http import Http404", 'raise Http404("gone")', 'raise NotFound("gone")'),
    ],
)
def test_every_frameworks_http_error_is_the_same_wreath_class(
    tmp_path, imports: str, call: str, expected: str
) -> None:
    source = f"{imports}\n\n\ndef read():\n    {call}\n"
    findings = _analyze(tmp_path, source)

    assert "port.http.exception" in _rules(findings)
    assert expected in _emit(tmp_path, source)


def test_tornados_log_message_is_not_published_to_the_caller(tmp_path) -> None:
    """`HTTPError(status, log_message)` -- the second positional goes to the log.

    Carrying it across as the detail would put an internal message in the body
    of every response. Only `reason=` is what the client was ever shown.
    """
    emitted = _emit(
        tmp_path,
        "import tornado.web\n"
        "\n"
        "\n"
        "def read():\n"
        '    raise tornado.web.HTTPError(409, "row %s is wedged", reason="clash")\n',
    )

    assert 'raise Conflict("clash")' in emitted
    assert "wedged" not in emitted


def test_a_status_with_no_wreath_class_stays_refused(tmp_path) -> None:
    """418 is in `docs/reference/port-gaps.md` as `exc.http_unmapped`.

    `wreath.exceptions` ships 400/401/403/404/405/409/413/422/429/431/500.
    Rounding a 418 to the nearest class that exists would change the status the
    caller sees, which is the whole content of the response.
    """
    source = "from flask import Flask, abort\n\n\ndef read():\n    abort(418)\n"
    findings = _analyze(tmp_path, source)

    assert "port.http.exception" not in _rules(findings)
    assert "foreign.flask.api" in _rules(findings)
    assert "abort(418)" in _emit(tmp_path, source)


def test_a_bare_abort_gains_the_raise_it_always_had(tmp_path) -> None:
    """Flask's `abort()` raises internally and reads like an ordinary call.

    Rewriting only the call would leave a statement that builds an exception and
    throws it away -- a route that answered 404 before the port and falls
    through to its own return after it.
    """
    emitted = _emit(
        tmp_path,
        "from flask import Flask, abort\n"
        "\n"
        "\n"
        "def read(plot_id):\n"
        "    if plot_id == 0:\n"
        "        abort(404)\n"
        '    return {"plot": plot_id}\n',
    )

    assert "        raise NotFound()" in emitted


# -- redirects -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("imports", "statement", "expected"),
    [
        (
            "from flask import Flask, redirect",
            'return redirect("/x", code=301)',
            'return RedirectResponse("/x", status=301)',
        ),
        (
            "from flask import Flask, redirect",
            'return redirect("/x")',
            'return RedirectResponse("/x", status=302)',
        ),
        (
            "from bottle import Bottle, redirect",
            'redirect("/x", 301)',
            'return RedirectResponse("/x", status=301)',
        ),
        (
            "from pyramid.httpexceptions import HTTPFound",
            'return HTTPFound(location="/x")',
            'return RedirectResponse("/x", status=302)',
        ),
        (
            "from django.http import HttpResponseRedirect",
            'return HttpResponseRedirect("/x")',
            'return RedirectResponse("/x", status=302)',
        ),
    ],
)
def test_a_redirect_never_loses_its_status(
    tmp_path, imports: str, statement: str, expected: str
) -> None:
    """Wreath's `RedirectResponse` defaults to 307 and none of these are.

    An omitted status is not "nothing to carry": 301 becoming 307 turns a
    permanent redirect into a temporary one, and 302 becoming 307 preserves the
    method, so a GET-after-POST becomes a re-POST.
    """
    source = f"{imports}\n\n\ndef read():\n    {statement}\n"

    assert "port.http.redirect" in _rules(_analyze(tmp_path, source))
    assert expected in _emit(tmp_path, source)


# -- routes --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("pattern", "signature", "expected_path", "expected_signature"),
    [
        ("/healthz", "()", '"/healthz"', "(request: Request)"),
        (
            "/plots/<int:plot_id>",
            "(plot_id)",
            '"/plots/{plot_id}"',
            "(request: Request, plot_id: int)",
        ),
        (
            "/q/<string:quadrat>",
            "(quadrat)",
            '"/q/{quadrat}"',
            "(request: Request, quadrat: str)",
        ),
        (
            "/n/<float:northing>",
            "(northing)",
            '"/n/{northing}"',
            "(request: Request, northing: float)",
        ),
        (
            "/a/<path:key>",
            "(key)",
            '"/a/{key:path}"',
            "(request: Request, key: str)",
        ),
    ],
)
def test_a_flask_path_converter_becomes_a_placeholder_and_an_annotation(
    tmp_path, pattern: str, signature: str, expected_path: str, expected_signature: str
) -> None:
    """The placeholder is `{name}` and the typing lives in the annotation.

    Both halves or neither: a path rewritten without its annotation binds the
    capture as a string, and an annotation without the path rewrite is a
    parameter nothing fills.
    """
    emitted = _emit(
        tmp_path,
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "\n"
        f'@app.route("{pattern}")\n'
        f"def read{signature}:\n"
        "    return {}\n",
    )

    assert f"@app.get({expected_path})" in emitted
    assert f"def read{expected_signature}:" in emitted


def test_a_bottle_converter_reads_the_other_way_round(tmp_path) -> None:
    """Flask writes `<int:id>` and Bottle writes `<id:int>`.

    Same brackets, opposite order. Reading one as the other produces a route
    parameter named after a type.
    """
    emitted = _emit(
        tmp_path,
        "from bottle import Bottle\n"
        "\n"
        "app = Bottle()\n"
        "\n"
        "\n"
        '@app.get("/sailings/<sailing_id:int>")\n'
        "def read(sailing_id):\n"
        "    return {}\n",
    )

    assert '@app.get("/sailings/{sailing_id}")' in emitted
    assert "def read(request: Request, sailing_id: int):" in emitted


@pytest.mark.parametrize(
    ("decorator", "expected"),
    [
        ('@app.route("/r")', '@app.get("/r")'),
        ('@app.route("/r", methods=["POST"])', '@app.post("/r")'),
        ('@app.route("/r", methods=["GET", "HEAD"])', '@app.get("/r")'),
        (
            '@app.route("/r", methods=["POST", "PUT"])',
            '@app.route("/r", methods=("POST", "PUT",))',
        ),
    ],
)
def test_the_methods_a_route_answers_pick_the_wreath_verb(
    tmp_path, decorator: str, expected: str
) -> None:
    """Flask's default is GET+HEAD, and a wreath GET route answers HEAD too.

    So the default is `@app.get`, not a loss -- and an explicit `["GET","HEAD"]`
    is the same route rather than two.
    """
    emitted = _emit(
        tmp_path,
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "\n"
        f"{decorator}\n"
        "def read():\n"
        "    return {}\n",
    )

    assert expected in emitted


def test_a_ported_handler_keeps_its_def(tmp_path) -> None:
    """Wreath dispatches a synchronous handler natively.

    This is a correctness point rather than a stylistic one: a WSGI handler's
    body is full of calls that block, and they were fine on a worker thread.
    Making it `async def` moves every one of them onto the event loop, which is
    the same mistake the gevent refusal exists to prevent.
    """
    emitted = _emit(
        tmp_path,
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "\n"
        '@app.route("/r")\n'
        "def read():\n"
        "    return {}\n",
    )

    assert "def read(request: Request):" in emitted
    assert "async def read" not in emitted


@pytest.mark.parametrize(
    ("pattern", "why"),
    [
        ("/u/<uuid:token>", "binding._convert_scalar has no uuid.UUID; it raises"),
        ("/c/<code:re:[A-Z]{2}>", "wreath's only converter is path, so this would widen"),
    ],
)
def test_a_converter_with_no_wreath_form_refuses_the_whole_route(
    tmp_path, pattern: str, why: str
) -> None:
    """Half a path is a route that answers on a URL nobody wrote.

    Downgrading `<code:re:[A-Z]{2}>` to `{code}` is the sharp one: it matches
    any single segment, so the port turns a 404 into a 200.
    """
    source = (
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "\n"
        f'@app.route("{pattern}")\n'
        "def read(token=None, code=None):\n"
        "    return {}\n"
    )
    findings = _analyze(tmp_path, source)

    assert "port.route.method" not in _rules(findings), why
    assert "foreign.flask.route" in _rules(findings)


def test_a_capture_the_handler_does_not_declare_refuses_the_route(tmp_path) -> None:
    """An annotation needs a parameter to sit on.

    This is what keeps aiohttp's handlers out of the rule: they take only
    `request` and read `match_info` in the body, so there is nothing in the
    signature to annotate and the capture would arrive nowhere.
    """
    findings = _analyze(
        tmp_path,
        "from aiohttp import web\n"
        "\n"
        "routes = web.RouteTableDef()\n"
        "\n"
        "\n"
        '@routes.get("/c/{consignment_id}")\n'
        "async def read(request):\n"
        '    return web.json_response({})\n',
    )

    assert "port.route.method" not in _rules(findings)
    assert "foreign.aiohttp.route" in _rules(findings)


# -- the application and its routers -------------------------------------------

def test_the_application_and_its_blueprints_become_wreath_objects(tmp_path) -> None:
    """`Flask(__name__)` passes an import name so Flask can find templates.

    Wreath finds neither templates nor static files that way -- `app.static` is
    explicit -- so the argument has nowhere to go and needs none. The
    blueprint's name becomes the router's `tags`, which is what the name was
    grouping by.
    """
    emitted = _emit(
        tmp_path,
        "from flask import Blueprint, Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        'plots = Blueprint("plots", __name__, url_prefix="/plots")\n'
        "\n"
        "app.register_blueprint(plots)\n",
    )

    assert "app = Wreath()" in emitted
    assert "plots = Router(prefix='/plots', tags=('plots',))" in emitted
    assert "app.include_router(plots)" in emitted


def test_a_blueprint_with_a_hook_of_its_own_is_not_a_router(tmp_path) -> None:
    """Wreath's hooks belong to the application, not to a router.

    A `before_request` registered on the blueprint runs for that blueprint's
    routes only; re-declared on the application it runs for all of them. That is
    a change to which requests it fires on, so the blueprint stays refused.
    """
    findings = _analyze(
        tmp_path,
        "from flask import Blueprint, Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        'plots = Blueprint("plots", __name__, url_prefix="/plots")\n'
        "\n"
        "\n"
        "@plots.before_request\n"
        "def load():\n"
        "    return None\n",
    )

    assert "port.router.new" not in _rules(findings)
    assert "foreign.flask.blueprint" in _rules(findings)


# -- the monkeypatch refuses the whole tree ------------------------------------

MONKEYPATCHED = """\
from gevent import monkey

monkey.patch_all()

from bottle import Bottle, abort, redirect

app = Bottle()


@app.get("/sailings/<sailing_id:int>")
def read(sailing_id):
    if sailing_id == 0:
        abort(404)
    return redirect("/x", 301)
"""


def test_a_monkeypatched_tree_translates_nothing_at_all(tmp_path) -> None:
    """Every construct here has an exact wreath spelling, and none of them fire.

    The plan is explicit that the measured Bottle cluster is Bottle *under
    gevent* and contributes nothing. A rewrite there produces code that passes
    its tests at low concurrency and serialises in production, so a tree that
    looks ported is worse than one that plainly is not -- and the gate is on the
    patch rather than on the framework, because the patch is what broke it.
    """
    (tmp_path / "app.py").write_text(MONKEYPATCHED)
    report = port.analyze(tmp_path)

    assert not any(f.rule_id.startswith("port.") for f in report.findings)
    assert report.detection.monkeypatched
    assert not report.detection.portable
    assert report.as_dict()["counts"]["translated"] == 0


def test_the_same_bottle_source_without_the_patch_does_translate(tmp_path) -> None:
    """The control. Nothing about Bottle is the problem; the patch is."""
    source = MONKEYPATCHED.replace(
        "from gevent import monkey\n\nmonkey.patch_all()\n\n", ""
    )
    findings = _analyze(tmp_path, source)

    assert {"port.app.wreath", "port.route.method", "port.http.exception"} <= _rules(
        findings
    )
