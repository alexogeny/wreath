"""Wreath recording — request capture, recording policies, and crash forensics.

Most of this module is *policy value types*, all deny-by-default: they describe
what a Forensic-mode recorder is allowed to retain and validate that a policy
stays inside its bounds. Deny-by-default is structural rather than a default
setting — the never-capture field classes below cannot be enabled through this
API at all, and every budget is bounded.

`read_ring_file` is the other side of the subsystem, and the only part of it
meant to be reached for *after* something has gone wrong. Given a path,
`TelemetryConfig.ring_path` maps the recorder's ring from a file rather than the
heap, so a process that dies badly leaves its last records readable; this reads
them back. It reports what it could not recover instead of raising, because a
file recovered from a crash is where a strict reader is least useful.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ._flight_schema import CaptureDisposition, CaptureFieldClass
from ._recording_format import (
    AttemptOutcome,
    AttemptRecord,
    BoundaryEvent,
    read_attempt_recording,
)
from ._ring_file import DecodedRing, RingRecord, read_ring_file

__all__ = [
    "BodyCapture",
    "RedactionPolicy",
    "CaptureBudget",
    "CapturePolicy",
    "TriggerKind",
    "Trigger",
    "RecordingPolicy",
    "RecordingPolicyError",
    "HeaderRule",
    "CompiledRedaction",
    "compile_redaction",
    "ActiveArm",
    "ArmRegistry",
    "DecodedRing",
    "RingRecord",
    "read_ring_file",
    "AttemptOutcome",
    "AttemptRecord",
    "AttemptTriggerKind",
    "AttemptTrigger",
    "AttemptPolicy",
    "AttemptRecorder",
    "BoundaryEvent",
    "BoundaryTrace",
    "read_attempt_recording",
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

_MAX_CAPTURE_BYTES = 1 << 30  # 1 GiB, matches telemetry's ceiling
_MAX_FIELDS = 1 << 16
_MAX_DEPTH = 64


class RecordingPolicyError(ValueError):
    """A recording/capture policy is invalid or would exceed a bound."""


def _validate_dispositions(
    allow: frozenset[str], hashed: frozenset[str], masked: frozenset[str],
    *, kind: str, forbidden: frozenset[str],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Lower-case and validate a name -> single-disposition mapping (headers or
    query parameters): forbidden names are refused, and a name may sit in at most
    one of the allow/hash/mask sets."""
    allow = frozenset(n.lower() for n in allow)
    hashed = frozenset(n.lower() for n in hashed)
    masked = frozenset(n.lower() for n in masked)
    banned = (allow | hashed | masked) & forbidden
    if banned:
        raise RecordingPolicyError(
            f"{kind}s {sorted(banned)} are never capturable and cannot be added to "
            "any redaction set"
        )
    for a, b, label in (
        (allow, hashed, "allowlist/hash"),
        (allow, masked, "allowlist/mask"),
        (hashed, masked, "hash/mask"),
    ):
        overlap = a & b
        if overlap:
            raise RecordingPolicyError(
                f"{kind}s {sorted(overlap)} appear in both the {label} sets; a "
                f"{kind} needs one disposition"
            )
    return allow, hashed, masked


def _within_sets(
    arm: tuple[frozenset[str], frozenset[str], frozenset[str]],
    ceiling: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> bool:
    """Whether an arm's (allow, hash, mask) name sets stay inside the ceiling's:
    an arm may hash/mask any name the ceiling reveals more of, never one it drops."""
    a_allow, a_hash, a_mask = arm
    c_allow, c_hash, c_mask = ceiling
    return (
        a_allow <= c_allow
        and a_hash <= (c_allow | c_hash)
        and a_mask <= (c_allow | c_hash | c_mask)
    )


class BodyCapture(StrEnum):
    """How a request/response body is retained."""

    NONE = "none"  # nothing
    METADATA = "metadata"  # length + media-type id only (default)
    HASHED = "hashed"  # metadata + keyed hash
    STRUCTURED = "structured"  # policy-selected fields, bounded depth/count


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Header/body redaction rules. Deny-by-default: an unlisted header keeps
    everything sensitive out. A header appears in at most one disposition set --
    allow (verbatim, bounded), hash (keyed fingerprint), or mask (length only) --
    and never in the forbidden classes above."""

    #: Lower-cased header names captured verbatim (bounded by the slab).
    header_allowlist: frozenset[str] = frozenset()
    #: Lower-cased header names retained as a process-local keyed hash.
    header_hash: frozenset[str] = frozenset()
    #: Lower-cased header names retained as length only (constant mask).
    header_mask: frozenset[str] = frozenset()
    #: Query-parameter names captured verbatim / hashed / length-only. Same
    #: deny-by-default, single-disposition model as headers, in their own
    #: namespace (a param named `authorization` is not a header).
    query_allowlist: frozenset[str] = frozenset()
    query_hash: frozenset[str] = frozenset()
    query_mask: frozenset[str] = frozenset()
    body: BodyCapture = BodyCapture.METADATA
    #: How dependency payloads (DB parameters, outbound request/response bodies)
    #: are retained. Deny-by-default: dependencies are captured only when an
    #: operator opts in, independently of the request/response body knob.
    dependency: BodyCapture = BodyCapture.NONE
    max_body_bytes: int = 0
    max_fields: int = 0
    max_depth: int = 0

    def __post_init__(self) -> None:
        allow, hashed, masked = _validate_dispositions(
            self.header_allowlist, self.header_hash, self.header_mask,
            kind="header", forbidden=NEVER_CAPTURE_HEADERS,
        )
        object.__setattr__(self, "header_allowlist", allow)
        object.__setattr__(self, "header_hash", hashed)
        object.__setattr__(self, "header_mask", masked)
        q_allow, q_hash, q_mask = _validate_dispositions(
            self.query_allowlist, self.query_hash, self.query_mask,
            kind="query parameter", forbidden=frozenset(),
        )
        object.__setattr__(self, "query_allowlist", q_allow)
        object.__setattr__(self, "query_hash", q_hash)
        object.__setattr__(self, "query_mask", q_mask)
        if not isinstance(self.body, BodyCapture):
            object.__setattr__(self, "body", BodyCapture(self.body))
        if not isinstance(self.dependency, BodyCapture):
            object.__setattr__(self, "dependency", BodyCapture(self.dependency))
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

    def narrow(self, other: RedactionPolicy) -> RedactionPolicy:
        """Combine two layers, keeping only what *both* permit. A lower config
        layer may narrow an inherited policy but never broaden it -- the compiled
        result is the intersection of the header sets, the least-revealing body,
        and the smaller byte bound (see the Stage-2 layered configuration model).
        """
        return RedactionPolicy(
            header_allowlist=self.header_allowlist & other.header_allowlist,
            header_hash=self.header_hash & other.header_hash,
            header_mask=self.header_mask & other.header_mask,
            query_allowlist=self.query_allowlist & other.query_allowlist,
            query_hash=self.query_hash & other.query_hash,
            query_mask=self.query_mask & other.query_mask,
            body=(
                self.body
                if _BODY_ORDER[self.body.value] <= _BODY_ORDER[other.body.value]
                else other.body
            ),
            dependency=(
                self.dependency
                if _BODY_ORDER[self.dependency.value] <= _BODY_ORDER[other.dependency.value]
                else other.dependency
            ),
            max_body_bytes=min(self.max_body_bytes, other.max_body_bytes),
            max_fields=min(self.max_fields, other.max_fields),
            max_depth=min(self.max_depth, other.max_depth),
        )


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
    #: Numeric threshold / route id / status; meaning depends on `kind`.
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
        """Whether a runtime capture stays inside this startup ceiling. A runtime
        arm may only capture headers/bodies the compiled ceiling already allows,
        with no more revealing a disposition and no larger a byte budget."""
        ceiling = self.redaction
        arm = capture.redaction
        within_bytes = capture.budget.total_bytes <= self.max_capture_bytes
        within_headers = _within_sets(
            (arm.header_allowlist, arm.header_hash, arm.header_mask),
            (ceiling.header_allowlist, ceiling.header_hash, ceiling.header_mask),
        )
        within_query = _within_sets(
            (arm.query_allowlist, arm.query_hash, arm.query_mask),
            (ceiling.query_allowlist, ceiling.query_hash, ceiling.query_mask),
        )
        within_body = arm.body.value in _BODY_ORDER and (
            _BODY_ORDER[arm.body.value] <= _BODY_ORDER[ceiling.body.value]
        )
        within_dependency = arm.dependency.value in _BODY_ORDER and (
            _BODY_ORDER[arm.dependency.value] <= _BODY_ORDER[ceiling.dependency.value]
        )
        return (
            within_bytes and within_headers and within_query
            and within_body and within_dependency
        )


# Ordering so a runtime arm cannot ask for a more revealing body than the ceiling.
_BODY_ORDER = {
    BodyCapture.NONE.value: 0,
    BodyCapture.METADATA.value: 1,
    BodyCapture.HASHED.value: 2,
    BodyCapture.STRUCTURED.value: 3,
}


# --- compiled capture plan --------------------------------------------------
#
# A RedactionPolicy is the human-facing rule set; the request-path capture seam
# needs a fast, immutable lookup from a field's identity to a native capture
# decision (deny, or a (disposition, descriptor_id) pair). Compilation happens
# once at startup -- never on the request path -- and interns header names to
# deterministic small integer descriptor ids (sorted, 1-based; 0 = none) so a
# captured field carries only an id and the reader resolves the name.


@dataclass(frozen=True, slots=True)
class HeaderRule:
    """The compiled capture decision for one header name."""

    descriptor_id: int
    disposition: CaptureDisposition


#: Native capture disposition per BodyCapture mode. METADATA keeps only the
#: length; HASHED keeps a keyed digest; STRUCTURED retains bounded raw content
#: (per-field structural selection is a later refinement); NONE drops the body.
_BODY_DISPOSITION: Mapping[str, CaptureDisposition | None] = {
    BodyCapture.NONE.value: None,
    BodyCapture.METADATA.value: CaptureDisposition.LENGTH,
    BodyCapture.HASHED.value: CaptureDisposition.HASHED,
    BodyCapture.STRUCTURED.value: CaptureDisposition.RAW,
}


@dataclass(frozen=True, slots=True)
class CompiledRedaction:
    """An immutable, deny-by-default capture plan compiled from a RedactionPolicy.

    The request-path seam looks a header name up here to get its native capture
    decision (or `None` to drop it), and reads a direction's body disposition
    and byte bound. This is the startup ceiling: a runtime arm resolves against
    the same plan and can only narrow it.
    """

    header_rules: Mapping[str, HeaderRule]
    #: descriptor_id (1-based) -> lower-cased header name, for the reader.
    header_names: tuple[str, ...]
    request_body: CaptureDisposition | None
    response_body: CaptureDisposition | None
    #: Disposition for dependency payloads (DB params, outbound bodies), or None.
    dependency_body: CaptureDisposition | None
    max_body_bytes: int
    #: Query-parameter rules, in their own descriptor namespace (like headers).
    query_rules: Mapping[str, HeaderRule] = field(default_factory=dict)
    query_names: tuple[str, ...] = ()

    def header(self, name: str) -> HeaderRule | None:
        """The capture decision for a header, or `None` to drop it (default)."""
        return self.header_rules.get(name.lower())

    def query(self, name: str) -> HeaderRule | None:
        """The capture decision for a query parameter, or `None` to drop it."""
        return self.query_rules.get(name.lower())

    def body(self, field_class: CaptureFieldClass) -> tuple[CaptureDisposition, int] | None:
        """The (disposition, max_bytes) for a body field class, or `None`."""
        if field_class is CaptureFieldClass.REQUEST_BODY:
            disposition = self.request_body
        elif field_class is CaptureFieldClass.RESPONSE_BODY:
            disposition = self.response_body
        else:
            return None
        return self._bound(disposition)

    def dependency(self) -> tuple[CaptureDisposition, int] | None:
        """The (disposition, max_bytes) for dependency payloads, or `None`.

        One knob covers every dependency field class (DB_PARAM, OUTBOUND_REQUEST,
        OUTBOUND_RESPONSE): they are all opaque payloads redacted the same way.
        """
        return self._bound(self.dependency_body)

    def _bound(
        self, disposition: CaptureDisposition | None
    ) -> tuple[CaptureDisposition, int] | None:
        if disposition is None:
            return None
        # LENGTH/HASHED ignore the byte bound (they retain no raw prefix).
        limit = self.max_body_bytes if disposition is CaptureDisposition.RAW else 0
        return disposition, limit


def compile_redaction(policy: RedactionPolicy) -> CompiledRedaction:
    """Compile a RedactionPolicy into an immutable capture plan.

    Header names are interned to deterministic descriptor ids (sorted, 1-based)
    so the same policy always produces the same ids regardless of set iteration
    order. Runs once at startup; never on the request path.
    """
    header_rules, header_names = _compile_sets(
        policy.header_allowlist, policy.header_hash, policy.header_mask, kind="header"
    )
    query_rules, query_names = _compile_sets(
        policy.query_allowlist, policy.query_hash, policy.query_mask,
        kind="query parameter",
    )
    body = _BODY_DISPOSITION[policy.body.value]
    return CompiledRedaction(
        header_rules=header_rules,
        header_names=header_names,
        request_body=body,
        response_body=body,
        dependency_body=_BODY_DISPOSITION[policy.dependency.value],
        max_body_bytes=policy.max_body_bytes,
        query_rules=query_rules,
        query_names=query_names,
    )


def _compile_sets(
    allow: frozenset[str], hashed: frozenset[str], masked: frozenset[str], *, kind: str,
) -> tuple[dict[str, HeaderRule], tuple[str, ...]]:
    """Intern a name -> disposition mapping to deterministic descriptor ids
    (sorted union, 1-based) so the same policy always compiles to the same ids."""
    ordered = sorted(allow | hashed | masked)
    if len(ordered) >= _MAX_FIELDS:
        raise RecordingPolicyError(
            f"{len(ordered)} capturable {kind}s exceed the {_MAX_FIELDS} descriptor cap"
        )
    rules: dict[str, HeaderRule] = {}
    names: list[str] = []
    for name in ordered:
        if name in allow:
            disposition = CaptureDisposition.RAW
        elif name in hashed:
            disposition = CaptureDisposition.HASHED
        else:
            disposition = CaptureDisposition.MASKED
        names.append(name)
        rules[name] = HeaderRule(descriptor_id=len(names), disposition=disposition)
    return rules, tuple(names)


# --- runtime arm registry ---------------------------------------------------
#
# A runtime capture arm is bounded and cannot broaden the startup ceiling. The
# Inspector installs arms into this registry (behind a capability token); the
# request-path capture seam consults the active arms and reports a match, which
# counts against the arm's budget and expiry. Both live on the event loop
# thread, so the registry is deliberately lock-free -- an arm swap happens at an
# event-loop-safe point, per the Stage-2 configuration model.


@dataclass(slots=True)
class _ArmState:
    arm_id: int
    capture: CapturePolicy
    compiled: CompiledRedaction
    expiry_monotonic: float
    max_matches: int  # 0 = unbounded (still bounded by expiry + the ceiling)
    matches: int = 0

    def is_live(self, now: float) -> bool:
        if now >= self.expiry_monotonic:
            return False
        return self.max_matches == 0 or self.matches < self.max_matches


@dataclass(frozen=True, slots=True)
class ActiveArm:
    """A read-only view of one installed arm, for the seam and CAPTURE_STATUS."""

    arm_id: int
    compiled: CompiledRedaction
    remaining_matches: int  # -1 = unbounded
    expires_in: float


class ArmRegistry:
    """Bounded set of live runtime capture arms, each inside the startup ceiling.

    `arm` refuses anything the compiled `RecordingPolicy` ceiling does
    not permit, requires a positive expiry (no "retain forever" runtime arm), and
    caps the number of concurrent arms. Expired or match-exhausted arms are
    pruned lazily on every read. Owned by the event-loop thread.
    """

    __slots__ = ("_ceiling", "_max_arms", "_arms", "_next_id", "_clock")

    def __init__(
        self,
        ceiling: RecordingPolicy,
        *,
        max_arms: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ceiling = ceiling
        self._max_arms = max_arms
        self._arms: dict[int, _ArmState] = {}
        self._next_id = 1
        self._clock = clock

    @property
    def ceiling(self) -> RecordingPolicy:
        return self._ceiling

    def arm(self, capture: CapturePolicy) -> ActiveArm:
        """Install a runtime arm, or raise if it would exceed the ceiling/limits."""
        if capture.expiry_seconds <= 0:
            raise RecordingPolicyError("a runtime capture arm must have expiry_seconds > 0")
        if not self._ceiling.permits(capture):
            raise RecordingPolicyError(
                "capture arm exceeds the startup redaction/memory ceiling"
            )
        now = self._clock()
        self._prune(now)
        if len(self._arms) >= self._max_arms:
            raise RecordingPolicyError(
                f"too many concurrent capture arms (max {self._max_arms})"
            )
        arm_id = self._next_id
        self._next_id += 1
        state = _ArmState(
            arm_id=arm_id,
            capture=capture,
            compiled=compile_redaction(capture.redaction),
            expiry_monotonic=now + capture.expiry_seconds,
            max_matches=capture.max_matches,
        )
        self._arms[arm_id] = state
        return self._view(state, now)

    def disarm(self, arm_id: int) -> bool:
        """Remove an arm. Returns whether it existed."""
        return self._arms.pop(arm_id, None) is not None

    def active(self, now: float | None = None) -> list[ActiveArm]:
        """The live arms, pruning any that expired or exhausted their matches."""
        now = self._clock() if now is None else now
        self._prune(now)
        return [self._view(state, now) for state in self._arms.values()]

    def note_match(self, arm_id: int) -> bool:
        """Record that an arm matched a request (the seam calls this). Returns
        whether the arm is still live afterward; a match-exhausted arm is removed."""
        state = self._arms.get(arm_id)
        if state is None:
            return False
        state.matches += 1
        if not state.is_live(self._clock()):
            del self._arms[arm_id]
            return False
        return True

    def _prune(self, now: float) -> None:
        dead = [aid for aid, state in self._arms.items() if not state.is_live(now)]
        for aid in dead:
            del self._arms[aid]

    def _view(self, state: _ArmState, now: float) -> ActiveArm:
        remaining = -1 if state.max_matches == 0 else state.max_matches - state.matches
        return ActiveArm(
            arm_id=state.arm_id,
            compiled=state.compiled,
            remaining_matches=remaining,
            expires_in=max(0.0, state.expiry_monotonic - now),
        )


# --- durable work: arming a job attempt --------------------------------------
#
# The request vocabulary above governs "which requests may be captured". This
# governs "which job attempts may be captured", with the same posture: an
# `AttemptPolicy` with no triggers captures nothing, and the tempting exception
# -- "surely a *failed* attempt is always worth keeping" -- is exactly the one
# that would make a queue full of personal data record itself by default.


class AttemptTriggerKind(StrEnum):
    """When an attempt is worth keeping."""

    #: Any outcome that is not completion: raised, deadline-cancelled, or
    #: lease-expired. The case worth most of this feature.
    FAILURE = "failure"
    #: A handler that raised, and only that. Distinct from FAILURE because a
    #: deadline cancellation is not a defect and a lease expiry is not either.
    RAISED = "raised"
    #: The attempt that exhausted `max_attempts` -- the one that dead-lettered.
    FINAL_FAILURE = "final_failure"
    #: Every outcome of one named task, optionally sampled. For a task under
    #: investigation, where the successes are as informative as the failures.
    TASK = "task"


@dataclass(frozen=True, slots=True)
class AttemptTrigger:
    """One arming predicate for a job attempt.

    `task` narrows any kind to one task name; `TASK` *requires* it, because a
    `TASK` trigger with no name means "record every attempt of every task" and
    this subsystem does not have a spelling for that.

    `rate` samples deterministically **from the job id**, never from an RNG: two
    workers looking at the same row have to agree on whether it is being
    recorded, and a re-run has to reach the same answer as the run it is
    reproducing.
    """

    kind: AttemptTriggerKind
    task: str = ""
    rate: float = 1.0

    def __post_init__(self) -> None:
        # Unconditional: `AttemptTriggerKind(member)` returns that member, so an
        # `isinstance` guard in front of this is two spellings of one condition
        # and only the guard would be tested.
        object.__setattr__(self, "kind", AttemptTriggerKind(self.kind))
        _require(0.0 <= self.rate <= 1.0, "trigger rate must be in [0, 1]")
        if self.kind is AttemptTriggerKind.TASK and not self.task:
            raise RecordingPolicyError(
                "a sampled task trigger names the task under investigation; an "
                "unnamed one is 'record every attempt', which is the opposite of "
                "this subsystem's posture"
            )

    def selects(
        self, *, task: str, outcome: AttemptOutcome, attempt: int, max_attempts: int
    ) -> bool:
        """Whether this trigger's *kind* matches, before sampling."""
        if self.task and self.task != task:
            return False
        if self.kind is AttemptTriggerKind.TASK:
            return True
        if outcome is AttemptOutcome.COMPLETED:
            return False
        if self.kind is AttemptTriggerKind.RAISED:
            return outcome is AttemptOutcome.RAISED
        if self.kind is AttemptTriggerKind.FINAL_FAILURE:
            return attempt >= max_attempts
        return True  # FAILURE: any outcome that is not completion


@dataclass(frozen=True, slots=True)
class AttemptPolicy:
    """What job attempts a runner may record. Deny-by-default: no triggers, no
    recordings.

    **Arguments are captured only where an operator named one, by task and
    parameter.** `args jsonb` is a *positional* array, so the name-keyed model
    the rest of `RedactionPolicy` uses has nothing to key on — and
    deny-by-default over a nameless unit degenerates to "capture nothing" or
    "capture everything", the second of which is the disclosure this subsystem
    ranks above correctness. `argument_allowlist` supplies the missing names
    from the *handler's signature*, which the recording process already holds:

    ```python
    AttemptPolicy(
        triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),),
        argument_allowlist=frozenset({"send_password_reset.user_id"}),
        redaction=RedactionPolicy(max_fields=32, max_depth=4, max_body_bytes=4096),
    )
    ```

    `send_password_reset(user_id, token)` then records `user_id` and never
    `token`, which is the whole point. Four rules make that safe, and each one
    fails **closed**:

    1. **No signature, no capture.** A task whose handler is not registered in
       this process — the dead-letter path already has one, from a release that
       accepted a different arity — has no names, so nothing is captured. The
       rule is *deny*, never fall back to position.
    2. **The mapping must be total and unambiguous.** A value that lands in
       `*args` or `**kwargs` maps to no declared parameter, so it is never
       captured however the allowlist is spelled.
    3. **The parameter is the unit of consent, and it is the whole argument.**
       Allowing `payload` allows everything inside it, bounded by the limits
       below. There is no per-field key space, because a path language whose
       leaves an operator has never seen is a consent nobody gave.
    4. **The value must normalise.** Strings, numbers, booleans, `None`, and
       lists/tuples/dicts of them, within `max_depth` and `max_fields` and
       `max_body_bytes`. Anything else — an object, `bytes`, a set, a cycle, an
       oversize structure — is **withheld with the reason recorded in its
       place**, so a reader can tell a refusal from an absence.

    A non-empty allowlist therefore needs those three bounds set; an
    `AttemptPolicy` that names an argument without them is refused where it is
    written. An empty allowlist is the default and records only the argument
    *count*, exactly as before.

    `max_boundaries` bounds one recording. Crossing it **refuses the recording**
    rather than truncating it: a job that walks ten thousand rows would
    otherwise produce a boundary trace that silently stops part-way, and a
    replay driven from it would report a different failure from the one that
    happened. The ring sets the same precedent with `RING_FULL`.
    """

    triggers: tuple[AttemptTrigger, ...] = ()
    redaction: RedactionPolicy = field(default_factory=RedactionPolicy.deny_by_default)
    max_boundaries: int = 512
    #: `"task.parameter"` keys. Split on the **last** dot, so a dotted task name
    #: still names one parameter.
    argument_allowlist: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _require(self.max_boundaries > 0, "max_boundaries must be > 0")
        _require(self.max_boundaries <= _MAX_FIELDS, "max_boundaries out of range")
        allowlist = frozenset(self.argument_allowlist)
        for key in sorted(allowlist):
            task, dot, parameter = key.rpartition(".")
            _require(
                bool(dot) and bool(task) and bool(parameter),
                f"argument allowlist entry {key!r} must be 'task.parameter'",
            )
        object.__setattr__(self, "argument_allowlist", allowlist)
        if allowlist:
            _require(
                self.redaction.max_fields > 0
                and self.redaction.max_depth > 0
                and self.redaction.max_body_bytes > 0,
                "capturing an argument needs redaction limits: set max_fields, "
                "max_depth and max_body_bytes, because an argument is a value of "
                "unknown shape and an unbounded one is a recording nobody can open",
            )

    def capture_arguments(
        self,
        *,
        task: str,
        handler: Any,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        framework_parameters: int = 0,
    ) -> tuple[tuple[str, str], ...]:
        """The allowlisted arguments of one call, normalised and bounded.

        Returns `(parameter_name, json_text)` pairs, where the JSON is exactly
        one of `{"value": ...}` or `{"withheld": "<reason>"}` — a refusal is
        recorded rather than dropped, because an operator who allowed a
        parameter and finds nothing cannot otherwise tell whether the job did
        not carry it or this refused it.

        Args:
            task: The task name, the left half of an allowlist key.
            handler: The registered callable, or `None` when this process has
                none — which captures nothing.
            args: Positional arguments, as the queue row carried them.
            kwargs: Keyword arguments, likewise.
            framework_parameters: Leading parameters the *runner* supplies
                rather than the payload — `JobRunner` calls
                `handler(ctx, *job.args)`, so it passes 1. They are aligned past
                and never capturable: `ctx` is this process's object, not
                anything the queue row carried, and an allowlist entry naming
                one records nothing.

        Returns:
            One pair per allowed parameter the call actually supplied, in
            signature order.
        """
        if not self.argument_allowlist or handler is None:
            return ()
        wanted = {
            key.rpartition(".")[2]
            for key in self.argument_allowlist
            if key.rpartition(".")[0] == task
        }
        if not wanted:
            return ()
        bound = _bind_arguments(handler, args, kwargs, framework_parameters)
        if bound is None:
            return ()
        limits = self.redaction
        out: list[tuple[str, str]] = []
        for name, value in bound:
            if name not in wanted:
                continue
            out.append((name, _normalise_argument(value, limits)))
        return tuple(out)

    def captures(
        self,
        *,
        task: str,
        outcome: AttemptOutcome,
        attempt: int,
        max_attempts: int,
        job_id: int,
    ) -> bool:
        """Whether this attempt is one an operator asked to keep."""
        if not isinstance(outcome, AttemptOutcome):
            outcome = AttemptOutcome(outcome)
        for trigger in self.triggers:
            if not trigger.selects(
                task=task, outcome=outcome, attempt=attempt, max_attempts=max_attempts
            ):
                continue
            if trigger.rate >= 1.0:
                return True
            if trigger.rate > 0.0 and sample_value(task, job_id) < trigger.rate:
                return True
        return False


#: Values an argument may be made of. Deliberately the JSON scalars and nothing
#: else: `args jsonb` is what the queue stored, so anything outside this set
#: arrived by some other route and its `repr` is not a thing to write to a
#: forensic file. `bool` is checked before `int` everywhere below, because it is
#: a subclass of one.
_ARGUMENT_SCALARS = (str, bool, int, float)


#: Stands in for a leading parameter the runner supplies. Never recorded: it is
#: dropped by name before any value is looked at.
_FRAMEWORK_ARGUMENT = object()


def _bind_arguments(
    handler: Any,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    framework_parameters: int = 0,
) -> list[tuple[str, Any]] | None:
    """`(parameter_name, value)` for a call, or `None` when that is not knowable.

    `None` -- capture nothing -- for a handler with no readable signature and
    for a call that does not bind to it. **A value that lands in `*args` or
    `**kwargs` is dropped rather than named**, because the name it would be
    given is a position or a caller's spelling and neither is the declared
    parameter an operator allowed.
    """
    import inspect

    target = getattr(handler, "__wrapped__", handler)
    leading = [_FRAMEWORK_ARGUMENT] * max(0, framework_parameters)
    try:
        signature = inspect.signature(target)
        bound = signature.bind(*leading, *args, **kwargs)
    except (TypeError, ValueError):
        # A builtin with no signature, a C callable, or a row enqueued by a
        # release whose handler took a different arity -- the dead-letter path
        # already has the second one. Deny, never guess.
        return None
    named: list[tuple[str, Any]] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if name not in bound.arguments:
            continue
        value = bound.arguments[name]
        if value is _FRAMEWORK_ARGUMENT:
            continue
        named.append((name, value))
    return named


def _normalise_argument(value: Any, limits: RedactionPolicy) -> str:
    """One argument as JSON text: `{"value": ...}` or `{"withheld": "<reason>"}`.

    Bounded four ways, and a breach of any of them withholds the *whole*
    argument rather than a truncated version of it. A half-recorded structure
    is the shape this subsystem refuses everywhere else -- a reader cannot tell
    a list of three from the first three of nine, and a replay driven off the
    short one reports a different failure from the one that happened.

    Immutable by construction: the normalised copy is built out of new lists
    and dicts and then serialised immediately, so a handler that mutates its
    own argument after this returns cannot change what was recorded.
    """
    import json

    try:
        copied = _copy_bounded(value, limits, depth=0, seen=set(), budget=[limits.max_fields])
    except _ArgumentRefused as refusal:
        return json.dumps({"withheld": str(refusal)})
    text = json.dumps({"value": copied}, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > limits.max_body_bytes:
        return json.dumps(
            {"withheld": f"over the {limits.max_body_bytes}-byte argument budget"}
        )
    return text


class _ArgumentRefused(Exception):
    """Why one argument could not be normalised. Never escapes this module."""


def _copy_bounded(
    value: Any, limits: RedactionPolicy, *, depth: int, seen: set[int], budget: list[int]
) -> Any:
    """An immutable-by-construction copy, or a refusal naming what stopped it."""
    if value is None or isinstance(value, _ARGUMENT_SCALARS):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            # JSON has no NaN or infinity and `allow_nan=False` would raise from
            # inside `json.dumps`, past the refusal path that records a reason.
            raise _ArgumentRefused("a non-finite number has no JSON form")
        return value
    if depth >= limits.max_depth:
        raise _ArgumentRefused(f"nested deeper than the {limits.max_depth}-level limit")
    if isinstance(value, list | tuple):
        # Identity, not equality: a cycle is what this catches, and two equal
        # sibling lists are not one. Removed on the way out so a value that
        # appears twice side by side is not mistaken for a cycle.
        if id(value) in seen:
            raise _ArgumentRefused("contains a cycle")
        seen.add(id(value))
        try:
            return [
                _copy_bounded(item, limits, depth=depth + 1, seen=seen, budget=_spend(budget))
                for item in value
            ]
        finally:
            seen.discard(id(value))
    if isinstance(value, dict):
        if id(value) in seen:
            raise _ArgumentRefused("contains a cycle")
        seen.add(id(value))
        try:
            copied: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _ArgumentRefused(
                        f"a mapping keyed by {type(key).__name__} has no JSON form"
                    )
                copied[key] = _copy_bounded(
                    item, limits, depth=depth + 1, seen=seen, budget=_spend(budget)
                )
            return copied
        finally:
            seen.discard(id(value))
    raise _ArgumentRefused(f"unsupported type {type(value).__name__}")


def _spend(budget: list[int]) -> list[int]:
    """Charge one field against the shared budget, or refuse.

    A single mutable cell rather than a per-level count, because the limit that
    matters is the size of the whole recorded value: a thousand one-element
    lists is the same file as one thousand-element list.
    """
    budget[0] -= 1
    if budget[0] < 0:
        raise _ArgumentRefused("more fields than the policy's max_fields allows")
    return budget


class BoundaryTrace:
    """The boundary crossings of one attempt, in order.

    Owned by the attempt that is running, so it needs no locking: a job handler
    is one coroutine on one event loop, and a crossing is written between two
    awaits of that coroutine.

    Crossing `max_boundaries` sets `overflowed` and stops recording. The
    recorder then **refuses to write the recording at all**, because a
    boundary trace that stops part-way replays as a different failure from the
    one that happened -- the fault would land at whatever statement is at that
    coordinate in the shorter run. Refusing is the same answer the ring gives
    with `RING_FULL`.
    """

    __slots__ = ("_events", "_next", "_max", "overflowed")

    def __init__(self, max_boundaries: int) -> None:
        self._events: list[BoundaryEvent] = []
        self._next: dict[tuple[int, str], int] = {}
        self._max = max_boundaries
        self.overflowed = False

    def note(self, seam: int, target: str) -> int:
        """Write down a crossing and hand back its coordinate.

        The coordinate advances even when the trace is full, so a replay driven
        from a *refused* recording could not accidentally be keyed to a
        renumbered position; nothing consumes an overflowed trace, and this
        keeps that true by construction rather than by convention.
        """
        key = (seam, target)
        coordinate = self._next.get(key, 0)
        self._next[key] = coordinate + 1
        if len(self._events) >= self._max:
            self.overflowed = True
            return -1
        self._events.append(BoundaryEvent(seam=seam, target=target, coordinate=coordinate))
        return len(self._events) - 1

    def fail(self, index: int, error_type: str) -> None:
        """Mark the crossing at `index` as having raised `error_type`."""
        if 0 <= index < len(self._events):
            self._events[index] = BoundaryEvent(
                seam=self._events[index].seam,
                target=self._events[index].target,
                coordinate=self._events[index].coordinate,
                error_type=error_type,
            )

    @property
    def events(self) -> tuple[BoundaryEvent, ...]:
        return tuple(self._events)


class AttemptRecorder:
    """Decides which job attempts are kept, and writes the ones that are.

    Hand one to `wreath.jobs.JobRunner(attempts=...)`. It arms nothing on its
    own: an `AttemptPolicy` with no triggers is the default and records nothing.

    Recordings land in `directory` as `<queue>-<job_id>-<attempt>.wfr1`, owner-
    only, one attempt per file. One attempt per file is not a storage decision:
    attempt 4 of a job is a different execution from attempt 3, and a reader
    that had to pick between them would be choosing which failure to reproduce.
    """

    __slots__ = (
        "_policy", "_directory", "_image", "scope",
        "written", "refused_oversize", "errors",
    )

    def __init__(
        self,
        policy: AttemptPolicy,
        *,
        directory: str,
        scope: object | None = None,
        image: object | None = None,
    ) -> None:
        self._policy = policy
        self._directory = directory
        self._image = image
        #: The application whose `_databases`/`_http_clients`/`_object_stores`
        #: an attempt is watched through, or None to watch only the runner's own
        #: database. A `JobRunner` has no reference to its application, and
        #: inventing one to record a job would put the recorder's needs into the
        #: queue's public shape.
        self.scope = scope
        #: Recordings written to disk.
        self.written = 0
        #: Attempts an operator armed and this refused to write because their
        #: boundary trace crossed `max_boundaries`. Counted rather than
        #: truncated: a recording nobody can open is still a recording, and one
        #: that quietly holds half a trace is not.
        self.refused_oversize = 0
        #: Recordings that could not be written -- a full disk, a missing
        #: directory. The attempt itself is untouched; only the evidence is.
        self.errors = 0

    @property
    def policy(self) -> AttemptPolicy:
        return self._policy

    def trace(self) -> BoundaryTrace:
        """A boundary trace bounded by this recorder's policy."""
        return BoundaryTrace(self._policy.max_boundaries)

    def captures(
        self,
        *,
        task: str,
        outcome: AttemptOutcome | str,
        attempt: int,
        max_attempts: int,
        job_id: int,
    ) -> bool:
        """Whether the policy arms for this attempt. See `AttemptPolicy.captures`."""
        return self._policy.captures(
            task=task,
            outcome=AttemptOutcome(outcome),
            attempt=attempt,
            max_attempts=max_attempts,
            job_id=job_id,
        )

    def write(self, record: AttemptRecord, trace: BoundaryTrace | None = None) -> str | None:
        """Write one attempt recording, or refuse it and say why in a counter.

        Returns the path written, or `None` when nothing was. Never raises: a
        recorder that can take a worker down with it is worse than no recorder,
        and the attempt it is describing has already happened.
        """
        import os

        from ._flight_schema import SCHEMA_VERSION, MetadataImage
        from ._recording_format import WFR1Writer

        if trace is not None and trace.overflowed:
            self.refused_oversize += 1
            return None
        image = self._image
        if image is None:
            image = MetadataImage(SCHEMA_VERSION, *([()] * 11))
        path = os.path.join(
            self._directory, f"{record.queue}-{record.job_id}-{record.attempt}.wfr1"
        )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                writer = WFR1Writer(handle, image)  # ty: ignore[invalid-argument-type]
                writer.write_attempt(record)
                writer.close()
        except OSError:
            # Narrow on purpose: a full disk or a missing directory is the
            # failure this survives. Anything else is a defect in the encoder
            # and must not be swallowed into a counter nobody reads.
            self.errors += 1
            return None
        self.written += 1
        return path


def sample_value(task: str, job_id: int) -> float:
    """A stable value in [0, 1) for one (task, job) pair.

    A checksum rather than a hash: `hash()` is salted per process, so two
    workers would disagree about the same row and a re-run would disagree with
    the run it is reproducing.
    """
    digest = zlib.crc32(f"{task}:{job_id}".encode()) & 0xFFFFFFFF
    return digest / 0x100000000


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecordingPolicyError(message)
