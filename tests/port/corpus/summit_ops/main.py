"""Summit Ops API — expedition logistics for a llama-trekking outfit.

Corpus fixture: a service that pairs a small FastAPI surface with DynamoDB-as-primary
persistence (no ORM), a websocket progress stream, and a batch CLI (see sibling modules).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel

from .repository import ExpeditionRepository, dynamo_repository
from .streaming import ws_router

app = FastAPI(title="Summit Ops", docs_url=None)
router = APIRouter(prefix="/expeditions", tags=["expeditions"])


class Expedition(BaseModel):
    id: str
    summit: str
    porters: int = 4
    gear: list[str] = []


def get_repo(request: Request) -> ExpeditionRepository:
    return dynamo_repository()


@router.get("")
async def list_expeditions(
    limit: int = Query(20, ge=1, le=100),
    repo: ExpeditionRepository = Depends(get_repo),
) -> list[Expedition]:
    return await repo.list(limit=limit)


@router.post("", status_code=201)
async def create_expedition(
    body: Expedition, repo: ExpeditionRepository = Depends(get_repo)
) -> Expedition:
    await repo.put(body)
    return body


app.include_router(router)
app.include_router(ws_router)
