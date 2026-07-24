"""Roost DTOs (plain Pydantic v2)."""
from pydantic import BaseModel


class AlpacaIn(BaseModel):
    name: str
    fleece_grade: str = "unclassified"


class AlpacaOut(BaseModel):
    id: str
    name: str
    fleece_grade: str
