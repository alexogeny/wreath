"""`as_form` — accept a pydantic model as multipart form fields.

A high-frequency idiom across the source repos, in both pydantic dialects.
"""
from __future__ import annotations

import inspect
from typing import Type

from fastapi import Form
from pydantic import BaseModel


def as_form(cls: Type[BaseModel]) -> Type[BaseModel]:
    """Decorator: synthesize an `as_form` classmethod binding the model from Form fields."""
    params = [
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_ONLY,
            default=Form(... if field.required else field.default),
            annotation=field.outer_type_,
        )
        for name, field in cls.__fields__.items()
    ]

    def _as_form(cls, **data):
        return cls(**data)

    _as_form.__signature__ = inspect.signature(_as_form).replace(parameters=params)
    setattr(cls, "as_form", classmethod(_as_form))
    return cls


@as_form
class GearManifestForm(BaseModel):
    expedition_id: str
    rope_metres: int = 60
    notes: str = ""
