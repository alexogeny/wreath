"""Collar tracking on a conservancy, built on wreath.

The camera-trap example next door is about a *place* that records what walks
past it. This one is about an *animal* that carries the recorder, and that one
change moves every hard problem: the payload is binary because a satellite link
charges by the byte, the coordinate is the record rather than a column beside
it, the data arrives days late when a collar loses the sky, and the answer a
reader gets depends on who is reading.

It is deliberately the smaller of the two examples. Routing, CRUD, paging, the
read API and migrations as artifacts are taught by the camera trap and are not
taught again here. What is here is ingest, place, policy and realtime.

Start at ``docs/tracking/index.md``.
"""

from __future__ import annotations

from .models import MODELS, SCHEMA

__all__ = ["MODELS", "SCHEMA"]
