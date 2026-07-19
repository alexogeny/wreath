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

from ._flight_schema import CaptureFieldClass, PhaseCoverage, PhaseKind

#: The armed request's bound ``_flight_phase``; read with ``.get(None)``.
phase_marker: ContextVar[Any] = ContextVar("wreath_flight_phase_marker")

#: The armed *Forensic* request's dependency capturer, bound only when a capture
#: arm is active and its narrowed policy permits dependency payloads. Read with
#: ``.get(None)`` and always nested inside the phase-marker gate, so a request
#: that is not Detailed-sampled never touches it. The bound callable takes
#: ``(field_class, data)`` and redacts per the arm's dependency disposition; an
#: escaped binding no-ops once the native context is severed at completion.
capture_marker: ContextVar[Any] = ContextVar("wreath_flight_capture_marker")

#: Plain-int phase and coverage identifiers so marker call sites never touch
#: the IntEnum machinery on a hot path.
PH_DB_POOL_WAIT = int(PhaseKind.DB_POOL_WAIT)
PH_DB_QUERY = int(PhaseKind.DB_QUERY)
PH_HTTP_CLIENT = int(PhaseKind.HTTP_CLIENT)
COV_PYTHON = int(PhaseCoverage.PYTHON)
COV_EXTERNAL = int(PhaseCoverage.EXTERNAL)

#: Plain-int dependency capture field classes for the seam call sites.
CAP_DB_PARAM = int(CaptureFieldClass.DB_PARAM)
CAP_DB_ROW = int(CaptureFieldClass.DB_ROW)
CAP_OUTBOUND_REQUEST = int(CaptureFieldClass.OUTBOUND_REQUEST)
CAP_OUTBOUND_RESPONSE = int(CaptureFieldClass.OUTBOUND_RESPONSE)

__all__ = [
    "CAP_DB_PARAM",
    "CAP_DB_ROW",
    "CAP_OUTBOUND_REQUEST",
    "CAP_OUTBOUND_RESPONSE",
    "COV_EXTERNAL",
    "COV_PYTHON",
    "PH_DB_POOL_WAIT",
    "PH_DB_QUERY",
    "PH_HTTP_CLIENT",
    "capture_marker",
    "phase_marker",
]
