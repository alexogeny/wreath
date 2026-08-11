"""Ledger and entry, in ormar.

``Custodian`` is declared in the shared ``fenland_core`` package rather than
here, so the foreign key on ``Entry`` names a model this tree cannot open — the
reason a lookup through that relation is not a rewrite anyone can perform from
this file alone.
"""
import uuid
from datetime import date

import ormar

from fenland_core.db import ledger_config
from fenland_core.models import Custodian


class Ledger(ormar.Model):
    ormar_config = ledger_config.copy(tablename="ledger")

    id: uuid.UUID = ormar.UUID(primary_key=True, default=uuid.uuid4)
    slug: str = ormar.String(max_length=64, nullable=False)
    currency: str = ormar.String(max_length=3, nullable=False)
    opened_on: date = ormar.Date(nullable=False)


class Entry(ormar.Model):
    ormar_config = ledger_config.copy(tablename="entry")

    id: uuid.UUID = ormar.UUID(primary_key=True, default=uuid.uuid4)
    ledger = ormar.ForeignKey(Ledger, nullable=False, related_name="entries")
    custodian = ormar.ForeignKey(Custodian, nullable=True, related_name="entries")
    amount_minor: int = ormar.Integer(nullable=False)
    posted_on: date = ormar.Date(nullable=False)
    memo: str = ormar.Text(nullable=True)
