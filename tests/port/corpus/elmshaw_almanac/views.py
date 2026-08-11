"""Elmshaw hedgerow almanac — the Pyramid half that has paths.

Pyramid is two frameworks in one package. Traversal has no URL patterns and
cannot be enumerated statically; URL dispatch does, and its path syntax is
`{name}` — character for character what wreath's router matches. This module is
entirely URL dispatch: every view names a route, every route is a literal
pattern registered in `routes.py`, and `renderer="json"` plus a returned dict is
already what a wreath handler does with no renderer at all.

The traversal half, the ACLs, the tweens and `config.scan()` are in
`foreign/mosswood_almanac/`.
"""

from dataclasses import dataclass

from pyramid.httpexceptions import HTTPConflict, HTTPFound, HTTPNotFound
from pyramid.view import view_config


@dataclass
class NewHedge:
    parish: str
    metres: int
    species: str


@view_config(route_name="healthz", renderer="json")
def healthz(request):
    return {"status": "ok"}


@view_config(route_name="hedge", renderer="json", request_method="GET")
def read_hedge(request):
    hedge_id = int(request.matchdict["hedge_id"])
    return {"hedge": hedge_id}


@view_config(route_name="hedge_survey", renderer="json", request_method="GET")
def read_survey(request):
    hedge_id = int(request.matchdict["hedge_id"])
    year = request.matchdict["year"]
    return {"hedge": hedge_id, "year": year}


@view_config(route_name="hedges", renderer="json", request_method="GET")
def search_hedges(request):
    parish = request.params.get("parish")
    limit = int(request.params.get("limit", "20"))
    return {"parish": parish, "limit": limit}


@view_config(route_name="hedges", renderer="json", request_method="POST")
def create_hedge(request):
    hedge = NewHedge(**request.json_body)
    request.response.status = 201
    return {"parish": hedge.parish, "metres": hedge.metres}


@view_config(route_name="hedge_measure", renderer="json", request_method="POST")
def measure_hedge(request):
    metres = int(request.POST.get("metres"))
    recorder = request.POST.get("recorder", "unattributed")
    return {"metres": metres, "recorder": recorder}


@view_config(route_name="hedge_trace", renderer="json")
def trace_hedge(request):
    trace = request.headers.get("X-Trace-Id")
    token = request.cookies.get("almanac_session")
    return {"trace": trace, "token": token}


@view_config(route_name="hedge_retire", renderer="json", request_method="DELETE")
def retire_hedge(request):
    hedge_id = request.matchdict["hedge_id"]
    if hedge_id == "0":
        raise HTTPNotFound("no such hedge")
    if hedge_id == "1":
        raise HTTPConflict("the reference hedge cannot be retired")
    return {"retired": hedge_id}


@view_config(route_name="hedge_legacy")
def legacy_hedge(request):
    return HTTPFound(location=f"/hedges/{request.matchdict['hedge_id']}")
