"""Wreath recording — request capture and recording policies.

Stage 0 of the Native Flight Recorder exposes *policy value types only*, all
deny-by-default. No capture happens: these objects describe what a later
Forensic-mode recorder would be allowed to retain, and validate that a policy
stays inside its bounds. The capture engine and ``WFR1`` sink land in Stage 5.

Deny-by-default is structural here: the never-capture field classes below cannot
be enabled through this API at all, and every budget is bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "BodyCapture",
    "RedactionPolicy",
    "CaptureBudget",
    "CapturePolicy",
    "TriggerKind",
    "Trigger",
    "RecordingPolicy",
    "RecordingPolicyError",
]

#: Header/field classes that are never captured, regardless of policy. This set
#: is enforced structurally: there is no switch to turn any of them on.
NEVER_CAPTURE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
    }
)

_MAX_CAPTURE_BYTES = 1 << 34  # 16 GiB, matches telemetry's ceiling
_MAX_FIELDS = 1 << 16
_MAX_DEPTH = 64


class RecordingPolicyError(ValueError):
    """A recording/capture policy is invalid or would exceed a bound."""


class BodyCapture(StrEnum):
    """How a request/response body is retained."""

    NONE = "none"  # nothing
    METADATA = "metadata"  # length + media-type id only (default)
    HASHED = "hashed"  # metadata + keyed hash
    STRUCTURED = "structured"  # policy-selected fields, bounded depth/count


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Header/body redaction rules. Deny-by-default: an empty allowlist keeps
    everything sensitive out."""

    #: Lower-cased header names explicitly allowed through (never the forbidden
    #: classes above).
    header_allowlist: frozenset[str] = frozenset()
    body: BodyCapture = BodyCapture.METADATA
    max_body_bytes: int = 0
    max_fields: int = 0
    max_depth: int = 0

    def __post_init__(self) -> None:
        lowered = frozenset(h.lower() for h in self.header_allowlist)
        forbidden = lowered & NEVER_CAPTURE_HEADERS
        if forbidden:
            raise RecordingPolicyError(
                f"headers {sorted(forbidden)} are never capturable and cannot be "
                "added to an allowlist"
            )
        object.__setattr__(self, "header_allowlist", lowered)
        if not isinstance(self.body, BodyCapture):
            object.__setattr__(self, "body", BodyCapture(self.body))
        _require(self.max_body_bytes >= 0, "max_body_bytes must be >= 0")
        _require(self.max_body_bytes <= _MAX_CAPTURE_BYTES, "max_body_bytes too large")
        _require(0 <= self.max_fields <= _MAX_FIELDS, "max_fields out of range")
        _require(0 <= self.max_depth <= _MAX_DEPTH, "max_depth out of range")
        if self.body is BodyCapture.STRUCTURED:
            _require(
                self.max_fields > 0 and self.max_depth > 0,
                "structured body capture needs max_fields and max_depth > 0",
            )

    @classmethod
    def deny_by_default(cls) -> RedactionPolicy:
        return cls()


@dataclass(frozen=True, slots=True)
class CaptureBudget:
    """Bounded capture storage. Exhaustion truncates/drops and increments a
    categorized counter; there is no unbounded retention."""

    slabs: int = 0
    slab_bytes: int = 64 * 1024
    per_request_bytes: int = 0
    per_route_bytes: int = 0

    def __post_init__(self) -> None:
        _require(self.slabs >= 0, "slabs must be >= 0")
        _require(self.slab_bytes >= 0, "slab_bytes must be >= 0")
        _require(self.per_request_bytes >= 0, "per_request_bytes must be >= 0")
        _require(self.per_route_bytes >= 0, "per_route_bytes must be >= 0")
        total = self.slabs * self.slab_bytes
        _require(total <= _MAX_CAPTURE_BYTES, "total capture budget exceeds the ceiling")

    @property
    def total_bytes(self) -> int:
        return self.slabs * self.slab_bytes


class TriggerKind(StrEnum):
    ERROR = "error"
    LATENCY = "latency"
    STATUS = "status"
    ROUTE = "route"
    TRACE = "trace"
    SAMPLE = "sample"
    TOKEN = "token"


@dataclass(frozen=True, slots=True)
class Trigger:
    """A compiled arming predicate. Route patterns resolve to IDs at compile
    time, never as request-time string matches."""

    kind: TriggerKind
    #: Numeric threshold / route id / status; meaning depends on ``kind``.
    value: int = 0
    #: Deterministic sample rate for SAMPLE triggers.
    rate: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TriggerKind):
            object.__setattr__(self, "kind", TriggerKind(self.kind))
        _require(0.0 <= self.rate <= 1.0, "trigger rate must be in [0, 1]")
        _require(self.value >= 0, "trigger value must be >= 0")


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    """What a single armed capture is allowed to retain and for how long."""

    redaction: RedactionPolicy = field(default_factory=RedactionPolicy.deny_by_default)
    budget: CaptureBudget = field(default_factory=CaptureBudget)
    triggers: tuple[Trigger, ...] = ()
    #: Runtime arm bounds; a runtime arm can never broaden the startup ceiling.
    expiry_seconds: float = 0.0
    max_matches: int = 0

    def __post_init__(self) -> None:
        _require(self.expiry_seconds >= 0, "expiry_seconds must be >= 0")
        _require(self.max_matches >= 0, "max_matches must be >= 0")


@dataclass(frozen=True, slots=True)
class RecordingPolicy:
    """The startup-compiled recording ceiling. Runtime arms select among these
    precompiled policies and cannot exceed the redaction/memory limits here."""

    capture_slabs: int = 0
    max_capture_bytes: int = 0
    redaction: RedactionPolicy = field(default_factory=RedactionPolicy.deny_by_default)

    def __post_init__(self) -> None:
        _require(self.capture_slabs >= 0, "capture_slabs must be >= 0")
        _require(self.max_capture_bytes >= 0, "max_capture_bytes must be >= 0")
        _require(
            self.max_capture_bytes <= _MAX_CAPTURE_BYTES,
            "max_capture_bytes exceeds the ceiling",
        )

    def permits(self, capture: CapturePolicy) -> bool:
        """Whether a runtime capture stays inside this startup ceiling."""
        within_bytes = capture.budget.total_bytes <= self.max_capture_bytes
        within_headers = capture.redaction.header_allowlist <= (
            self.redaction.header_allowlist
        )
        within_body = capture.redaction.body.value in _BODY_ORDER and (
            _BODY_ORDER[capture.redaction.body.value]
            <= _BODY_ORDER[self.redaction.body.value]
        )
        return within_bytes and within_headers and within_body


# Ordering so a runtime arm cannot ask for a more revealing body than the ceiling.
_BODY_ORDER = {
    BodyCapture.NONE.value: 0,
    BodyCapture.METADATA.value: 1,
    BodyCapture.HASHED.value: 2,
    BodyCapture.STRUCTURED.value: 3,
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecordingPolicyError(message)
