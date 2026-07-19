"""Armed-request phase-marker propagation to dependency seams.

Dispatch binds the native context's ``_flight_phase`` here only when the
request was sampled into Detailed (``flight == 2``), so every other request
pays exactly one ``ContextVar.get(None)`` per dependency call and nothing
else. Two properties make the binding safe to read from anywhere:

- The native server runs each request in its own task, and a task's context
  dies with it, so a marker set during dispatch cannot leak into a later
  request on the same connection.
- A binding that escapes the request anyway (captured by a spawned task or a
  background hook) degrades to an inert no-op: the protocol severs the
  context's borrowed recorder pointers when the completion is published
  (``wreath_request_context_sever``), and ``_flight_phase`` checks them.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from ._flight_schema import PhaseCoverage, PhaseKind

#: The armed request's bound ``_flight_phase``; read with ``.get(None)``.
phase_marker: ContextVar[Any] = ContextVar("wreath_flight_phase_marker")

#: Plain-int phase and coverage identifiers so marker call sites never touch
#: the IntEnum machinery on a hot path.
PH_DB_POOL_WAIT = int(PhaseKind.DB_POOL_WAIT)
PH_DB_QUERY = int(PhaseKind.DB_QUERY)
PH_HTTP_CLIENT = int(PhaseKind.HTTP_CLIENT)
COV_PYTHON = int(PhaseCoverage.PYTHON)
COV_EXTERNAL = int(PhaseCoverage.EXTERNAL)

__all__ = [
    "COV_EXTERNAL",
    "COV_PYTHON",
    "PH_DB_POOL_WAIT",
    "PH_DB_QUERY",
    "PH_HTTP_CLIENT",
    "phase_marker",
]
