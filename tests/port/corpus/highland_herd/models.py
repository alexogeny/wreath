"""Ormar models using the ``ormar_config = base.copy(...)`` idiom.

The other spelling (a nested ``class Meta``) lives in ``driftwood_gateway``;
this root exercises the newer one, plus a self-referencing FK and a JSON column.
"""
import uuid
from datetime import datetime, timezone

import ormar

from highland_core.db import base_ormar_config


class Paddock(ormar.Model):
    ormar_config = base_ormar_config.copy(tablename="paddock")

    id: uuid.UUID = ormar.UUID(primary_key=True, default=uuid.uuid4)
    name: str = ormar.String(max_length=255, nullable=False)
    hectares: float = ormar.Float(nullable=False)
    altitude_m: int = ormar.Integer(nullable=True)


class Rider(ormar.Model):
    ormar_config = base_ormar_config.copy(tablename="rider")

    id: uuid.UUID = ormar.UUID(primary_key=True, default=uuid.uuid4)
    handle: str = ormar.String(max_length=64, nullable=False)
    joined_on = ormar.Date(nullable=False)


class Llama(ormar.Model):
    ormar_config = base_ormar_config.copy(tablename="llama")

    id: uuid.UUID = ormar.UUID(primary_key=True, default=uuid.uuid4)
    name: str = ormar.String(max_length=255, nullable=False)
    paddock = ormar.ForeignKey(Paddock, nullable=True, related_name="llamas")
    grade: int = ormar.Integer(nullable=False, default=1)
    fleece_kg: float = ormar.Float(nullable=True)
    traits = ormar.JSON(nullable=True)
    retired: bool = ormar.Boolean(default=False, nullable=False)
    created_at: datetime = ormar.DateTime(
        default=datetime.now(tz=timezone.utc), timezone=True, nullable=False
    )


class Trek(ormar.Model):
    ormar_config = base_ormar_config.copy(tablename="trek")

    id: uuid.UUID = ormar.UUID(primary_key=True, default=uuid.uuid4)
    llama = ormar.ForeignKey(Llama, nullable=False, related_name="treks")
    rider = ormar.ForeignKey(Rider, nullable=True, related_name="treks")
    started_at: datetime = ormar.DateTime(timezone=True, nullable=False)
    distance_km: float = ormar.Float(nullable=False)
    notes: str = ormar.Text(nullable=True)
