"""Neutral domain models for the workload suite.

Deliberately generic (``Widget``, ``Quotation``) so the framework carries no
third-party benchmark's table or model names. A thin external adapter can map
these to any prescribed conformance schema without touching src/wreath.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Widget:
    """A small record used for point reads and fan-out reads."""

    id: int
    value: int


@dataclass(frozen=True, slots=True)
class Quotation:
    """A short text record rendered through an escaped HTML table."""

    id: int
    message: str


@dataclass(frozen=True, slots=True)
class WidgetUpdate:
    """One read-modify-write unit: apply ``value`` to widget ``id``."""

    id: int
    value: int
