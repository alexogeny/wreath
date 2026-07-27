"""The HTTP surface: routes, status constants, response classes, auth schemes.

Exercises the declarative tail a mature FastAPI app accumulates — explicit
``Body(...)`` markers, ``status.HTTP_*`` constants rather than integers, a
``response_class`` override, a streaming export, and a bearer scheme threaded
through ``Security``.
"""
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Security, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader, HTTPBearer
from pydantic import BaseModel, Field

from . import repository
from .caching import grade_band

router = APIRouter(prefix="/llamas", tags=["llamas"])

bearer = HTTPBearer()
api_key = APIKeyHeader(name="x-herd-key")


class LlamaIn(BaseModel):
    name: str
    paddock_id: str
    grade: int = Field(default=1, ge=1, le=5)


class LlamaOut(BaseModel):
    id: str
    name: str
    grade: int
    band: str
    notes: Optional[str] = None


async def current_rider(credentials=Security(bearer)):
    rider = await repository.find_rider(credentials.credentials)
    if rider is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown rider")
    return rider


@router.get("/", response_model=list[LlamaOut])
async def list_llamas(
    paddock_id: str = Query(...),
    min_grade: int = Query(1, ge=1, le=5),
    rider=Depends(current_rider),
):
    llamas = await repository.llamas_in_paddock(paddock_id)
    return [
        {"id": str(x.id), "name": x.name, "grade": x.grade, "band": grade_band(x.grade)}
        for x in llamas
        if x.grade >= min_grade
    ]


@router.get("/{llama_id}", response_model=LlamaOut)
async def read_llama(llama_id: str, rider=Depends(current_rider)):
    llama = await repository.find_llama(llama_id)
    if llama is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such llama")
    return jsonable_encoder(llama)


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_llama(payload: LlamaIn, rider=Depends(current_rider)):
    llama = await repository.enrol_llama(payload.name, payload.paddock_id)
    return JSONResponse({"id": str(llama.id)}, status_code=201)


@router.patch("/{llama_id}/grade")
async def regrade(llama_id: str, grade: int = Body(..., embed=True)):
    llama = await repository.require_paddock(llama_id)
    llama.grade = grade
    await llama.update()
    return {"ok": True}


@router.get("/export.csv", response_class=StreamingResponse)
async def export_llamas(request: Request):
    async def rows():
        for llama in await repository.every_paddock():
            yield f"{llama.id},{llama.name}\n".encode()

    return StreamingResponse(rows(), media_type="text/csv")
