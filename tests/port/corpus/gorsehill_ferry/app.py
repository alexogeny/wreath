"""Gorsehill ferry timetable — Bottle, and no monkeypatch anywhere.

The measured Bottle cluster is a gevent tree, where refusal is correct: under
`patch_all()` nothing below can be ported mechanically. This module separates
the two claims. There is no `gevent` import here, so what is left is ordinary
Bottle, and ordinary Bottle is close to wreath in the two places that matter:
a handler already returns a dict and gets JSON, and the route decorators are
spelled `@app.get`/`@app.post`.

What changes is the path converters (`<sailing_id:int>` is `{sailing_id}` plus
an `int` annotation), the module-level `request`/`response` proxies (each read
by a constant key is a bound parameter), and `abort`/`redirect`.
"""

from bottle import Bottle, abort, redirect, request, response

app = Bottle()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/sailings/<sailing_id:int>")
def read_sailing(sailing_id):
    return {"sailing": sailing_id}


@app.get("/sailings/<sailing_id:int>/legs/<leg>")
def read_leg(sailing_id, leg):
    return {"sailing": sailing_id, "leg": leg}


@app.get("/routes/<name:path>")
def read_route(name):
    return {"route": name}


@app.get("/sailings")
def search_sailings():
    port = request.query.get("port")
    limit = int(request.query.get("limit", "20"))
    return {"port": port, "limit": limit}


@app.post("/sailings")
def create_sailing():
    payload = request.json
    response.status = 201
    return {"port": payload["port"]}


@app.post("/sailings/<sailing_id:int>/board")
def board_sailing(sailing_id):
    passengers = int(request.forms.get("passengers"))
    return {"sailing": sailing_id, "passengers": passengers}


@app.get("/sailings/<sailing_id:int>/trace")
def trace_sailing(sailing_id):
    trace = request.headers.get("X-Trace-Id")
    token = request.get_cookie("ferry_session")
    return {"trace": trace, "token": token}


@app.route("/sailings/<sailing_id:int>", method="DELETE")
def cancel_sailing(sailing_id):
    if sailing_id == 1:
        abort(409, "the scheduled crossing cannot be cancelled")
    if sailing_id > 9999:
        abort(404)
    return {"cancelled": sailing_id}


@app.get("/legacy/sailings/<sailing_id:int>")
def legacy_sailing(sailing_id):
    redirect(f"/sailings/{sailing_id}", 301)
