"""Foxglove dispatch — the FastAPI needs-review shapes, each next to its
near-miss twin.

Every pair below is one construct written twice: once in the form that carries
across on its own, and once in the form that does not. The pairs are the
specification — a rewrite that fires on both is a rewrite that has guessed.

* `response_class=` — `docs/reference/port-gaps.md` puts this at 3 sites under
  "not a gap, just unwritten": delete the keyword, return the type.
* `status_code=` on a route whose handler returns a plain value is
  `JSONResponse(value, status=n)`; on a route that already returns a response
  object, the route-level value was doing nothing.
* `first()` — needs an order. With `order_by` in the chain, the order is
  supplied and `fetch_one(...order_by(...).limit(1))` is exact; without one,
  "the first row" is whatever postgres returned that day.
* `get_or_create` — static field values expand onto `fetch_one` plus `create`;
  a `defaults=` computed at the call site does not.
* `HTTPException(status_code=...)` with a literal is the matching wreath class;
  with a computed status it is not.
"""

from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .models import Catchment, Manifest

app = FastAPI()
router = APIRouter(prefix="/catchments", tags=["catchments"])


@router.get("/{catchment_id}", response_class=JSONResponse)
async def read_catchment(catchment_id: int):
    return {"catchment": catchment_id}


@router.get("/{catchment_id}/manifest")
async def read_manifest(catchment_id: int):
    return JSONResponse({"catchment": catchment_id})


@router.post("/{catchment_id}/manifests", status_code=201)
async def create_manifest(catchment_id: int, reference: str):
    return {"catchment": catchment_id, "reference": reference}


@router.post("/{catchment_id}/seal", status_code=202)
async def seal_manifest(catchment_id: int):
    return JSONResponse({"sealed": catchment_id}, status_code=202)


@router.get("/{catchment_id}/latest")
async def latest_manifest(catchment_id: int):
    return await Manifest.objects.filter(catchment=catchment_id).order_by("-created").first()


@router.get("/{catchment_id}/any")
async def any_manifest(catchment_id: int):
    return await Manifest.objects.filter(catchment=catchment_id).first()


@router.post("/{catchment_id}/ensure")
async def ensure_catchment(catchment_id: int, slug: str):
    return await Catchment.objects.get_or_create(id=catchment_id, slug=slug)


@router.post("/{catchment_id}/ensure-dynamic")
async def ensure_catchment_dynamic(catchment_id: int, slug: str):
    return await Catchment.objects.get_or_create(id=catchment_id, defaults={"slug": slug.strip()})


@router.get("/{catchment_id}/audit")
async def audit_catchment(catchment_id: int, stage: int):
    if stage == 0:
        raise HTTPException(status_code=404, detail="no such catchment")
    raise HTTPException(status_code=_status_for(stage), detail="unavailable")


@router.get("/")
async def search_catchments(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    slug: Annotated[str, Query(min_length=3)] = "",
):
    return {"limit": limit, "slug": slug}


def _status_for(stage: int) -> int:
    return 409 if stage > 3 else 400


app.include_router(router)
