"""Ormar models — the herd, ranches, bookings, and pricing.

Exercises: tablename via ``.copy(tablename=)``, a mixin, ForeignKey, an enum,
a JSONB column, a Postgres array column, nullable columns, ``server_default``,
and a UUID primary key with ``default_factory``.
"""
import enum
import uuid

import ormar
import sqlalchemy
from ormar_postgres_extensions.fields import ARRAY

from tumbleweed_core import base_ormar_config, generate_guid


class AuditMixin:
    created_at = ormar.DateTime(timezone=True, server_default=sqlalchemy.func.now())
    updated_at = ormar.DateTime(timezone=True, nullable=True)


class TrekStatus(str, enum.Enum):
    DRAFT = "draft"
    BOOKED = "booked"
    UNDERWAY = "underway"
    COMPLETE = "complete"


class Ranch(ormar.Model, AuditMixin):
    ormar_config = base_ormar_config.copy(tablename="ranch")

    id: uuid.UUID = ormar.UUID(primary_key=True, default_factory=generate_guid)
    name: str = ormar.String(max_length=200)
    slug: str = ormar.String(max_length=80, unique=True)
    settings: dict = ormar.JSON(nullable=True)


class Llama(ormar.Model, AuditMixin):
    ormar_config = base_ormar_config.copy(tablename="llama")

    id: uuid.UUID = ormar.UUID(primary_key=True, default_factory=generate_guid)
    ranch: Ranch = ormar.ForeignKey(Ranch, index=True)
    name: str = ormar.String(max_length=120)
    temperament: str = ormar.String(max_length=40, nullable=True)
    pack_weight_kg: int = ormar.Integer(minimum=0, server_default="0")
    tags: list = ARRAY(item_type=sqlalchemy.String())
    pack_manifest: dict = ormar.JSON(nullable=True)


class Booking(ormar.Model, AuditMixin):
    ormar_config = base_ormar_config.copy(tablename="booking")

    id: uuid.UUID = ormar.UUID(primary_key=True, default_factory=generate_guid)
    ranch: Ranch = ormar.ForeignKey(Ranch, index=True)
    llama: Llama = ormar.ForeignKey(Llama, index=True)
    status: str = ormar.String(
        max_length=20, choices=list(TrekStatus), default=TrekStatus.DRAFT.value
    )
    guests: int = ormar.Integer(minimum=1)
    notes: str = ormar.Text(nullable=True)


class RateCard(ormar.Model, AuditMixin):
    ormar_config = base_ormar_config.copy(tablename="rate_card")

    id: uuid.UUID = ormar.UUID(primary_key=True, default_factory=generate_guid)
    ranch: Ranch = ormar.ForeignKey(Ranch, index=True)
    name: str = ormar.String(max_length=120)
    cents_per_day: int = ormar.Integer(minimum=0)
    card_metadata: dict = ormar.JSON(nullable=True)
