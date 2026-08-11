"""Foxglove dispatch models — ormar, so `api.py` next door has a query surface.

Nothing here is new evidence; the ormar model and column rules are already
`translated` and `emit/ormar.py` writes them out. They exist so the `.objects`
chains in `api.py` resolve against real columns and a real relation, which is
what decides whether `query_rule()` calls a filter exact.
"""

import ormar

from .database import foxglove_db


class Catchment(ormar.Model):
    ormar_config = foxglove_db.copy(tablename="catchment")

    id: int = ormar.Integer(primary_key=True)
    slug: str = ormar.String(max_length=64, unique=True)
    name: str = ormar.String(max_length=200)


class Manifest(ormar.Model):
    ormar_config = foxglove_db.copy(tablename="manifest")

    id: int = ormar.Integer(primary_key=True)
    catchment: Catchment = ormar.ForeignKey(Catchment)
    reference: str = ormar.String(max_length=120)
    created: str = ormar.String(max_length=40)
