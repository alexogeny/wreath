"""Hollowbeck moorland survey — the Flask shapes that stay refused.

The companion to `corpus/alderbrook_survey/`. Everything here is spelled the
same way and is *not* a rewrite, so a rule that widens to cover Alderbrook must
still refuse every line below. Each stanza carries one reason, and the reason is
a property of wreath, not of the analyzer:

* `<uuid:...>` — `binding._convert_scalar` converts `str`, `int`, `float`,
  `bool`, `Instant`/`datetime` and `date`, and nothing else. A path parameter
  annotated `uuid.UUID` fails at request time, so the converter has no target.
* `getlist` — `Query` "takes the first value" of a repeated key
  (`binding.py:1307`). A multi-valued query parameter has no binding.
* `g` and `before_request` — a value stashed on the request-context proxy is
  read by name from another function. Which of parameter, middleware state or
  contextvar it becomes is a design decision.
* `session[...]` — Flask's signed-cookie session is a store, not a cookie read.
* a dynamic argument key — nothing static declares the parameter.
"""

import uuid

from flask import Blueprint, Flask, g, jsonify, request, session

app = Flask(__name__)
moor = Blueprint("moor", __name__, url_prefix="/moor")

FILTERABLE = ("heather", "bracken", "sphagnum")


@app.before_request
def attach_surveyor():
    g.surveyor = request.headers.get("X-Surveyor", "unassigned")


@moor.get("/transects/<uuid:transect_id>")
def read_transect(transect_id: uuid.UUID):
    return jsonify(transect=str(transect_id), surveyor=g.surveyor)


@moor.get("/species")
def filter_species():
    tags = request.args.getlist("tag")
    return jsonify(tags=tags)


@moor.get("/dynamic")
def dynamic_filters():
    chosen = {name: request.args.get(name) for name in FILTERABLE}
    return jsonify(chosen=chosen)


@moor.post("/basket")
def remember_basket():
    session["basket"] = request.form.get("plot")
    return jsonify(remembered=session["basket"])


@moor.get("/teapot")
def teapot():
    # 418 has no wreath exception class; see docs/reference/port-gaps.md,
    # `exc.http_unmapped`. This is a known wreath gap, not a porter bug.
    return jsonify(error="short and stout"), 418


app.register_blueprint(moor)
