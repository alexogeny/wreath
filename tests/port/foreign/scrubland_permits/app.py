"""Grazing permits for a commons association.

Flask's binding model is what does not carry across. The handler takes no
request; it reads a module-level proxy that resolves per request, and whatever
a ``before_request`` hook left on ``g`` is in scope everywhere without being a
parameter anywhere. The path converters (``<int:permit_id>``) are Flask's own
syntax, and the blueprint's ``url_prefix`` is re-declared rather than translated.
"""
from flask import Blueprint, Flask, abort, current_app, g, jsonify, request

app = Flask(__name__)
permits = Blueprint("permits", __name__, url_prefix="/permits")


@app.before_request
def load_commons():
    g.commons = request.headers.get("X-Commons", "upper")
    g.registry = current_app.config["REGISTRY_URL"]


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@permits.route("/<int:permit_id>", methods=["GET"])
def show_permit(permit_id):
    permit = lookup(g.commons, permit_id)
    if permit is None:
        abort(404)
    return jsonify(permit)


@permits.post("/<int:permit_id>/renew")
def renew_permit(permit_id):
    seasons = request.get_json().get("seasons", 1)
    return jsonify(permit=permit_id, renewed=seasons), 202


@app.teardown_request
def close_registry(exc):
    registry = g.pop("registry", None)
    if registry is not None:
        registry.close()


def lookup(commons, permit_id):
    raise NotImplementedError("wired up by the application factory")


app.register_blueprint(permits)
