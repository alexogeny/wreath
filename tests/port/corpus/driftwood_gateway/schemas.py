"""Request/response DTOs for the integration gateway."""
from typing import Optional

from pydantic import BaseModel, Field


class ProviderCreate(BaseModel):
    ranch_id: str
    endpoint: str
    kind: str = Field(default="collar")
    name: str
    api_key: str


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: str
    endpoint: str


class SightingIn(BaseModel):
    collar_serial: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    note: Optional[str] = None
