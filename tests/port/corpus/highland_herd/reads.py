"""Read endpoints for the herd, and the repository function behind them.

Two layers, deliberately: a route handler, where wreath can supply a session,
and a repository function, where it cannot without changing every caller.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse, PlainTextResponse

from .models import Llama

router = APIRouter(prefix="/reads", tags=["reads"])


@router.get("/llamas")
async def llamas_in_herd(herd: str, limit: int = Query(default=20, ge=1, le=100)):
    """Every lookup here carries straight across."""
    rows = await Llama.objects.filter(herd=herd, age__gte=2).order_by("-age").limit(limit).all()
    total = await Llama.objects.filter(herd=herd).count()
    return JSONResponse(content={"rows": len(rows), "total": total}, status_code=status.HTTP_200_OK)


@router.get("/llamas/search")
async def search_llamas(term: str = Query(...)):
    """A pattern lookup rewrites the value, which is still a translation."""
    rows = await Llama.objects.filter(name__icontains=term).all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no llama")
    return PlainTextResponse(f"{len(rows)} llamas", status_code=200)


@router.get("/llamas/retired")
async def retired_llamas():
    """No parameters at all, and a lookup whose value decides the call."""
    return await Llama.objects.filter(retired_at__isnull=False).all()


async def llamas_with_ranch(herd: str):
    """A repository read: no session in scope until someone threads one in."""
    return await Llama.objects.select_related("ranch").filter(herd=herd).all()
