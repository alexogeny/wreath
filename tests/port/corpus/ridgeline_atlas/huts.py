"""Hut availability and bed holds.

Four route options FastAPI accepted and wreath spells somewhere else: a status
on a handler whose return value cannot be read here, a 204 that returns a body
anyway, a response class on the decorator, and length and pattern rules on
query parameters. The weather endpoint raises two statuses no wreath exception
class covers on its own.
"""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ridgeline_core.store import fetch_hut, list_huts, place_bed_hold, release_hold

router = APIRouter(prefix="/huts", tags=["huts"])


@router.get("", response_class=PlainTextResponse)
async def hut_index(
    request: Request,
    massif: str = Query("", min_length=2, max_length=32),
    slug_prefix: str = Query(None, pattern=r"^[a-z][a-z-]*$"),
):
    return "\n".join(list_huts(massif, slug_prefix))


@router.post("/{hut_id}/holds", status_code=201)
async def place_hold(hut_id: str, nights: int = 1):
    hold = await place_bed_hold(hut_id, nights)
    return hold


@router.delete("/{hut_id}/holds/{hold_id}", status_code=204)
async def drop_hold(hut_id: str, hold_id: str):
    await release_hold(hut_id, hold_id)
    return {"released": hold_id}


@router.get("/{hut_id}/weather")
async def hut_weather(hut_id: str):
    report = await fetch_hut(hut_id)
    if report is None:
        raise HTTPException(status_code=503, detail="the summit feed is offline")
    if report.stale:
        raise HTTPException(
            status_code=429,
            detail="refreshed too often",
            headers={"Retry-After": "30"},
        )
    return report.summary
