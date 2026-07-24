"""Ranches router — the multi-tenant workspace surface."""
from fastapi import APIRouter, Depends, Request

from ..dependencies import current_wrangler
from ..models import RateCard, Ranch

router = APIRouter(prefix="/ranches", tags=["ranches"])


@router.get("")
async def list_ranches(request: Request, wrangler=Depends(current_wrangler)):
    return await Ranch.objects.all()


@router.get("/{slug}/rate-cards")
async def ranch_rate_cards(slug: str):
    ranch = await Ranch.objects.get(slug=slug)
    return (
        await RateCard.objects.filter(ranch__id=ranch.id)
        .order_by("cents_per_day")
        .all()
    )
