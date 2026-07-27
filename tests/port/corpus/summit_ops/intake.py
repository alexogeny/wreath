"""Gear-intake routes — a handler that consumes the ``as_form`` model, a
``status_code`` handler that returns a DTO, and a ``response_model`` handler.

The middle one is the ``status_code`` case that must NOT be auto-wrapped: the
return is a model instance, and ``JSONResponse(<dataclass>)`` raises in wreath.
"""
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
