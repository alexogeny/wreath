"""Roost — alpaca boarding. SQLModel / SQLAlchemy models (a different ORM stack).

Exercises the SQLModel spelling of the ORM-model translation rules: ``table=True``,
``__tablename__``, ``Field(primary_key=)``, ``foreign_key=``, ``sa_column`` with a
``server_default``, and nullable via ``Optional``.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel


class Alpaca(SQLModel, table=True):
    __tablename__ = "alpaca"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=120)
    fleece_grade: Optional[str] = Field(default=None)
    boarded_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=func.now())
    )


class Stall(SQLModel, table=True):
    __tablename__ = "stall"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    alpaca_id: Optional[uuid.UUID] = Field(default=None, foreign_key="alpaca.id")
    label: str = Field(max_length=40)
    occupied: bool = Field(default=False)
