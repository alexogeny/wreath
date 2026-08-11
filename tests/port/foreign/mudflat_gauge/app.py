"""Tide-gauge readings for a coastal survey group.

Flask spells its route decorator `@app.route(...)` and its blueprints
`@bp.get(...)` — the second of which is character-for-character what FastAPI
writes. `g` and `request` are module-level proxies that resolve per request,
which is a different binding model from a parameter, and neither is visible as
one at any call site.
"""

from flask import Blueprint, Flask, g, jsonify, request

app = Flask(__name__)
gauges = Blueprint("gauges", __name__, url_prefix="/gauges")


@app.before_request
def attach_station():
    g.station = request.args.get("station", "default")


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@gauges.get("/<int:gauge_id>")
def read_gauge(gauge_id):
    rows = current_db().readings(gauge_id, g.station)
    return jsonify(gauge=gauge_id, readings=rows)


@gauges.post("/<int:gauge_id>")
def record_gauge(gauge_id):
    height = float(request.form.get("height", "0"))
    current_db().record(gauge_id, g.station, height)
    return jsonify(ok=True), 201


@app.errorhandler(404)
def not_found(exc):
    return jsonify(error="not found"), 404


def current_db():
    if not hasattr(g, "db"):
        g.db = connect()
    return g.db


def connect():
    raise NotImplementedError("wired up in the application factory")


app.register_blueprint(gauges)
