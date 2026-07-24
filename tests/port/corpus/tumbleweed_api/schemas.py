"""Pydantic v2 request/response DTOs.

Exercises: ``extra="forbid"``, ``Field(ge=, le=)``, a ``= []`` default,
``@field_validator`` and ``@model_validator``.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BookingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llama_id: str
    guests: int = Field(ge=1, le=12)
    notes: Optional[str] = None
    add_ons: list = []

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value):
        return value.strip() if value else value


class RangeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int
    end: int

    @model_validator(mode="after")
    def check_range(self):
        if self.end < self.start:
            raise ValueError("end before start")
        return self


class LlamaOut(BaseModel):
    id: str
    name: str
    temperament: Optional[str] = None
    tags: list = []
