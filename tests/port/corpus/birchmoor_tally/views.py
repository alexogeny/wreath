"""Birchmoor deer-count views — function-based, so each one is a route.

A Django function view is already the shape of a wreath handler: it takes the
request first and the captured URL parameters after it, by name. What changes is
the return, and every return here has one spelling: `JsonResponse(d)` is
`return d`, `HttpResponse(status=204)` is `Response(status=204)`, and `Http404`
is `NotFound`.

`urls.py` next door carries the other half — `path("ranges/<int:range_id>/")`
uses the same converter vocabulary Flask does, and `int`, `str`, `slug` and
`path` all land on a wreath placeholder plus an annotation.
"""

import json
from dataclasses import dataclass

from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse

from .models import Observer, Range, Tally


@dataclass
class NewTally:
    species: str
    counted: int
    confidence: int = 100


def healthz(request):
    return JsonResponse({"status": "ok"})


def read_range(request, range_id):
    return JsonResponse({"range": range_id})


def read_range_by_slug(request, slug):
    return JsonResponse({"slug": slug})


def search_ranges(request):
    name = request.GET.get("name")
    limit = int(request.GET.get("limit", "20"))
    return JsonResponse({"name": name, "limit": limit})


def record_tally(request, range_id):
    payload = json.loads(request.body)
    tally = NewTally(**payload)
    return JsonResponse({"range": range_id, "species": tally.species}, status=201)


def weigh_tally(request, tally_id):
    counted = request.POST.get("counted")
    return JsonResponse({"tally": tally_id, "counted": counted})


def trace_range(request, range_id):
    trace = request.headers.get("X-Trace-Id")
    token = request.COOKIES.get("tally_session")
    return JsonResponse({"trace": trace, "token": token})


def retire_range(request, range_id):
    if range_id == 1:
        raise Http404("the reference range cannot be retired")
    return HttpResponse(status=204)


def legacy_range(request, range_id):
    return HttpResponseRedirect(f"/ranges/{range_id}/")


def read_attachment(request, key):
    return JsonResponse({"key": key})


def observer_summary(request, observer_id):
    observer = Observer.objects.get(id=observer_id)
    tallies = Tally.objects.filter(observer_id=observer_id).count()
    return JsonResponse({"observer": observer.display_name, "tallies": tallies})


def range_summary(request, range_id):
    found = Range.objects.filter(id=range_id).first()
    if found is None:
        raise Http404("no such range")
    return JsonResponse({"range": found.slug})
