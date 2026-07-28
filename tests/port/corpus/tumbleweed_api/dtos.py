"""Request and response models for the llama endpoints.

Covers the `Field(...)` forms in use here — a plain default, a factory, a
required marker, a marker carrying only documentation, one carrying a bound, and
one carrying an alias — plus a field order pydantic tolerates.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LlamaSummary(BaseModel):
    """Documentation-only markers: every one of these is just a default."""

    name: str = Field(..., description="the llama's name")
    herd: str = Field(description="which herd it walks with")
    fleece_kg: float = Field(default=0.0, description="last shear weight")
    tags: list[str] = Field(default_factory=list, description="free-form labels")
    retired: bool = Field(False)


class LlamaFilter(BaseModel):
    """A required field after a defaulted one — legal here, illegal as a dataclass."""

    page: int = 1
    herd: str


class LlamaBounds(BaseModel):
    """A bound has three possible homes in wreath, so it stays for a human."""

    age: int = Field(..., ge=0, le=40, description="years")
    height_cm: int = Field(default=100, ge=0)


class LlamaWire(BaseModel):
    """An alias renames the field on the wire, which is never silent."""

    model_config = ConfigDict(extra="ignore")

    fleece_kg: float = Field(default=0.0, alias="fleeceKg")
