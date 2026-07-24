"""Legacy ascent-plan models — pydantic v1 dialect.

Distinct from the v2 fixtures: @validator/@root_validator, confloat, and `class Config`.
"""
from __future__ import annotations

from pydantic import BaseModel, confloat, root_validator, validator


class AscentPlan(BaseModel):
    summit: str
    target_altitude_m: confloat(gt=0, le=9000)
    porters: int = 4
    oxygen_litres: float = 0.0

    class Config:
        orm_mode = True
        allow_population_by_field_name = True

    @validator("summit")
    def _summit_titlecase(cls, v):
        return v.title()

    @root_validator
    def _require_oxygen_high(cls, values):
        if values.get("target_altitude_m", 0) > 6000 and values.get("oxygen_litres", 0) <= 0:
            raise ValueError("high-altitude ascent requires oxygen")
        return values
