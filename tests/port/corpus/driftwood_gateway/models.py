"""Ormar models using the nested ``class Meta(OrmarMeta)`` idiom + UniqueColumns.

Exercises the alternative ormar spelling (a ``Meta`` inner class rather than
``ormar_config = base.copy(...)``), a composite unique constraint, an indexed FK
string, ``server_default``, and soft-delete nullable columns.
"""
import uuid
from datetime import datetime, timezone

import ormar

from driftwood_core.db import OrmarMeta


class Provider(ormar.Model):
    class Meta(OrmarMeta):
        tablename = "provider"
        constraints = [ormar.UniqueColumns("name", "deleted_at")]

    id: uuid.UUID = ormar.UUID(primary_key=True, default_factory=uuid.uuid4)
    ranch_id: str = ormar.String(index=True, max_length=255, nullable=False)
    endpoint: str = ormar.String(max_length=255, nullable=False)
    kind: str = ormar.String(max_length=64, nullable=False)
    name: str = ormar.String(max_length=255, nullable=False)
    api_key: str = ormar.String(max_length=255, nullable=False)
    created_at: datetime = ormar.DateTime(
        default=datetime.now(tz=timezone.utc), timezone=True, nullable=False
    )
    deleted: bool = ormar.Boolean(default=False, server_default="false", nullable=False)
    deleted_at: datetime = ormar.DateTime(nullable=True, timezone=True)


class Collar(ormar.Model):
    class Meta(OrmarMeta):
        tablename = "collar"

    id: uuid.UUID = ormar.UUID(primary_key=True, default_factory=uuid.uuid4)
    provider: Provider = ormar.ForeignKey(Provider, index=True)
    serial: str = ormar.String(max_length=120, unique=True)
    last_seen: datetime = ormar.DateTime(nullable=True, timezone=True)
