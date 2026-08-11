"""Alderbrook wetland survey — the Flask shapes that have a wreath spelling.

Every construct here is one wreath already expresses. The route decorators are
literal paths, the converters are the three whose types wreath converts, and
every read of the request proxy names a key that a bound parameter would name.
Nothing in this module reads `g`, sets up a request context, or registers a
hook — those live in `foreign/hollowbeck_survey/`, which is the boundary.

The claim is: a Flask route whose path is a literal, whose converters are
`int`/`float`/`path`/`string`, and whose body reads `request.args`,
`request.form`, `request.headers`, `request.cookies` or `request.get_json()` by
constant key, is a wreath route function and a parameter list. It is a rewrite,
not a port.
"""

from dataclasses import dataclass

from flask import Blueprint, Flask, abort, jsonify, redirect, request

app = Flask(__name__)
plots = Blueprint("plots", __name__, url_prefix="/plots")


@dataclass
class QuadratReading:
    quadrat: str
    cover_percent: int
    surveyor: str


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@app.route("/reports", methods=["POST"])
def submit_report():
    return jsonify(accepted=True), 202


@plots.get("/<int:plot_id>")
def read_plot(plot_id):
    return jsonify(plot=plot_id)


@plots.get("/<int:plot_id>/quadrats/<string:quadrat>")
def read_quadrat(plot_id, quadrat):
    return jsonify(plot=plot_id, quadrat=quadrat)


@plots.get("/<float:northing>/nearest")
def nearest_plot(northing):
    return jsonify(northing=northing)


@plots.get("/attachments/<path:key>")
def read_attachment(key):
    return jsonify(key=key)


@plots.get("/search")
def search_plots():
    name = request.args.get("name")
    limit = request.args.get("limit", 20, type=int)
    include_retired = request.args.get("include_retired", False, type=bool)
    return jsonify(name=name, limit=limit, retired=include_retired)


@plots.post("/<int:plot_id>/quadrats")
def record_quadrat(plot_id):
    quadrat = request.form.get("quadrat")
    cover = request.form.get("cover_percent", type=int)
    return jsonify(plot=plot_id, quadrat=quadrat, cover=cover), 201


@plots.post("/<int:plot_id>/readings")
def post_reading(plot_id):
    payload = request.get_json()
    reading = QuadratReading(**payload)
    return jsonify(plot=plot_id, quadrat=reading.quadrat)


@plots.get("/<int:plot_id>/trace")
def trace_plot(plot_id):
    trace = request.headers.get("X-Trace-Id")
    session_cookie = request.cookies.get("survey_session")
    return jsonify(plot=plot_id, trace=trace, session=session_cookie)


@plots.post("/<int:plot_id>/photo")
def upload_photo(plot_id):
    photo = request.files["photo"]
    return jsonify(plot=plot_id, filename=photo.filename)


@plots.get("/<int:plot_id>/exists")
def plot_exists(plot_id):
    if plot_id > 9999:
        abort(404)
    return "", 204


@plots.delete("/<int:plot_id>")
def retire_plot(plot_id):
    if plot_id == 1:
        abort(409, description="the reference plot cannot be retired")
    return jsonify(retired=plot_id)


@plots.get("/<int:plot_id>/legacy")
def legacy_plot(plot_id):
    return redirect(f"/plots/{plot_id}", code=301)


@app.errorhandler(404)
def not_found(exc):
    return jsonify(error="not found"), 404


app.register_blueprint(plots)
