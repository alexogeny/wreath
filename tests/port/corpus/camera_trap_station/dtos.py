"""Generated projections and request validation."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .models import Camera


class CameraCreate(Camera.get_pydantic(include={"serial", "trail"})):
    active: bool = True


class ReadingUnits(BaseModel):
    distance: Literal["m", "km"] = "m"
    confidence: int = Field(default=0, ge=0, le=100, description="match confidence")
    camera_label: str = Field(alias="cameraLabel", max_length=80)

    @field_validator("distance")
    @classmethod
    def distance_unit(cls, value):
        if value not in {"m", "km"}:
            raise ValueError("unknown distance unit")
        return value
