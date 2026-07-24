"""Gear-intake routes — a handler that consumes the ``as_form`` model, a single-return
``status_code`` handler, and a ``response_model`` handler (all now translated)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .forms import GearManifestForm

router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("", status_code=201)
async def submit(manifest: GearManifestForm = Depends(GearManifestForm.as_form)):
    return manifest


@router.get("/{expedition_id}", response_model=GearManifestForm)
async def latest(expedition_id: str):
    return GearManifestForm(expedition_id=expedition_id)
