"""Llamas router.

Exercises: ``Query(20, ge=1, le=100, alias=...)``, Header/Cookie/Form/File
markers, an ``UploadFile``, ``include_in_schema=False``, a path ``int`` param,
``Model.get_pydantic(include=...)`` metaprogramming, and JSONB ``jsonb_has_any``
/ ``jsonb_contains`` query lookups.
"""
from typing import Optional

from fastapi import APIRouter, Cookie, File, Form, Header, Query, Request, UploadFile

from ..models import Llama
from ..schemas import LlamaOut
from ..storage import store_manifest

router = APIRouter(prefix="/llamas", tags=["llamas"])

LlamaSummary = Llama.get_pydantic(include={"id", "name", "temperament"})


@router.get("", response_model=list, include_in_schema=False)
async def list_llamas(
    request: Request,
    limit: int = Query(20, ge=1, le=100, alias="pageSize"),
    temperament: Optional[str] = Query(None),
    x_ranch: str = Header(...),
    session_id: Optional[str] = Cookie(None),
):
    query = Llama.objects.filter(ranch__slug=x_ranch)
    if temperament:
        query = query.filter(temperament=temperament)
    return await query.limit(limit).all()


@router.get("/search")
async def search_by_tags(request: Request, tags: str):
    wanted = tags.split(",")
    return await Llama.objects.filter(tags__jsonb_has_any=wanted).all()


@router.get("/manifest-holders")
async def manifest_holders(request: Request, needle: dict):
    return await Llama.objects.filter(pack_manifest__jsonb_contains=needle).all()


@router.post("/{llama_id}/manifest")
async def upload_manifest(
    llama_id: str, note: str = Form(...), file: UploadFile = File(...)
):
    body = await file.read()
    key = store_manifest(llama_id, body)
    return {"stored": key, "note": note}


@router.get("/{llama_id}", response_model=LlamaOut)
async def get_llama(llama_id: int):
    llama = await Llama.objects.get_or_none(id=llama_id)
    return llama
