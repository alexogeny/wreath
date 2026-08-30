"""Read-only Unix-socket Inspector for Native Flight Recorder snapshots."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import socket
import stat
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, cast

from ._flight_schema import (
    FLAG_AI_SCRAPING_REFUSED,
    FLAG_POLICY_REFUSED,
    LossReason,
    Protocol,
)

MAGIC = b"WFI1"
PROTOCOL_VERSION = 1
HEADER = struct.Struct("!4sBBHII")
FLAG_ERROR = 1 << 0
FLAG_TRUNCATED = 1 << 1

#: One command per frame, one frame in flight per connection.
MAX_PAYLOAD_BYTES = 64 * 1024
#: Rows per page, applied to ACTIVE_REQUESTS and METADATA.
MAX_PAGE_ROWS = 256


class Command(IntEnum):
    HELLO = 1
    WORKERS = 2
    ACTIVE_REQUESTS = 3
    PRESSURE = 4
    EXPLAIN_ROUTE = 5
    EXPLAIN_PLAN = 6
    METADATA = 7
    TIMELINE = 8
    RECENT_FAILURES = 9
    ROUTE_DISTRIBUTIONS = 10
    ARM_CAPTURE = 11
    DISARM_CAPTURE = 12
    CAPTURE_STATUS = 13


_CORE_COMMANDS = frozenset(
    {
        Command.HELLO,
        Command.WORKERS,
        Command.ACTIVE_REQUESTS,
        Command.PRESSURE,
        Command.EXPLAIN_ROUTE,
        Command.EXPLAIN_PLAN,
        Command.METADATA,
    }
)
_PROJECTION_COMMANDS = frozenset(
    {Command.TIMELINE, Command.RECENT_FAILURES, Command.ROUTE_DISTRIBUTIONS}
)
_CAPTURE_COMMANDS = frozenset({Command.ARM_CAPTURE, Command.DISARM_CAPTURE, Command.CAPTURE_STATUS})

_METADATA_TABLES = (
    "routes",
    "plans",
    "dependencies",
    "middleware",
    "auth_policies",
    "serializers",
    "validators",
    "limits",
    "clients",
    "databases",
    "models",
    "components",
)


class InspectorError(Exception):
    """A protocol-level failure reported by the server or detected locally."""


@dataclass(frozen=True, slots=True)
class InspectorConfig:
    """Where (and whether) the read-only Inspector listens.

    The Inspector is off unless a config is provided. `path` must live in a
    directory the owning user controls; the socket itself is created 0600.
    """

    path: str
    max_payload_bytes: int = MAX_PAYLOAD_BYTES
    idle_timeout: float = 30.0
    #: Shared secret gating the mutating capture-control commands. When unset,
    #: capture control is disabled entirely (the commands are neither advertised
    #: nor answered), so a read-only Inspector can never arm capture.
    capture_token: str | None = None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("inspector socket path cannot be empty")
        if self.max_payload_bytes <= 0 or self.max_payload_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError("max_payload_bytes must be in (0, 64 KiB]")
        if self.idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive")
        if self.capture_token is not None and len(self.capture_token) < 16:
            raise ValueError("capture_token must be at least 16 characters")


@dataclass(frozen=True, slots=True)
class ActiveRequest:
    """One in-flight request from an active-table snapshot."""

    request_id: int
    age_us: int
    protocol: str
    route_id: int


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    """Counters and gauges for one recorder worker."""

    mode: int
    requests: int
    completions: int
    active_count: int
    ring_occupancy: int
    ring_high_water: int
    phase_capacity: int
    phase_in_use: int
    phase_high_water: int
    losses: dict[str, int] = field(default_factory=dict)


def _worker_payload(recorder: Any) -> dict[str, Any]:
    return {
        "mode": int(recorder.mode),
        "requests": recorder.requests,
        "completions": recorder.completions,
        "active_count": recorder.active_count,
        "ring_occupancy": recorder.ring_occupancy,
        "ring_high_water": recorder.ring_high_water,
        "phase_capacity": recorder.phase_capacity,
        "phase_in_use": recorder.phase_in_use,
        "phase_high_water": recorder.phase_high_water,
        "losses": {reason.name.lower(): recorder.loss(int(reason)) for reason in LossReason},
    }


def _protocol_name(value: int) -> str:
    try:
        return Protocol(value).name.lower()
    except ValueError:
        return "unknown"


def _metadata_name(names: dict[int, str], identifier: int) -> str:
    try:
        return names[identifier]
    except KeyError:
        return str(identifier)


class InspectorServer:
    """Serves the read-only protocol beside a recorder, inside the app process."""

    def __init__(
        self,
        recorder: Any,
        app: Any,
        config: InspectorConfig,
        projector: Any = None,
        arm_registry: Any = None,
    ) -> None:
        self._recorder = recorder
        self._app = app
        self._config = config
        self._projector = projector
        # Capture control needs both a registry and a configured token; without
        # either, the commands are neither advertised nor answered.
        self._arm_registry: Any = arm_registry if config.capture_token else None
        self._server: asyncio.AbstractServer | None = None
        self._image: Any = None
        self._routes_by_id: dict[int, Any] | None = None
        self._routes_by_key: dict[tuple[str, str], Any] | None = None
        self._plans_by_id: dict[int, Any] | None = None
        self._metadata_names: dict[str, dict[int, str]] | None = None

    def _capabilities(self) -> list[str]:
        """The commands this server answers. Projection-backed and capture-control
        commands appear only when their machinery is attached, so clients can
        feature-test rather than guess."""
        available = _CORE_COMMANDS | (
            _PROJECTION_COMMANDS if self._projector is not None else frozenset()
        )
        if self._arm_registry is not None:
            available = available | _CAPTURE_COMMANDS
        return [command.name for command in Command if command in available]

    @property
    def path(self) -> str:
        return self._config.path

    async def start(self) -> None:
        path = self._config.path
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            # Replace only something that is already a socket; refuse to unlink
            # an arbitrary file a hostile link may have planted at the path.
            if not stat.S_ISSOCK(existing.st_mode):
                raise InspectorError(f"inspector path exists and is not a socket: {path}")
            os.unlink(path)
        previous_umask = os.umask(0o177)  # socket file lands as 0600
        try:
            self._server = await asyncio.get_running_loop().create_unix_server(
                lambda: _InspectorProtocol(self), path
            )
        finally:
            os.umask(previous_umask)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self._config.path)
        except OSError:
            pass

    def _metadata_image(self) -> Any:
        image = self._image
        if image is None:
            from ._flight_metadata import build_metadata_image

            image = build_metadata_image(self._app)
            self._image = image
        if self._routes_by_id is None:
            self._routes_by_id = {route.route_id: route for route in image.routes}
            self._routes_by_key = {(route.method, route.path): route for route in image.routes}
            self._plans_by_id = {plan.plan_id: plan for plan in image.plans}
            self._metadata_names = {
                table: {entry.entry_id: entry.name for entry in getattr(image, table)}
                for table in (
                    "dependencies",
                    "middleware",
                    "auth_policies",
                    "serializers",
                    "validators",
                    "limits",
                )
            }
        return self._image

    def _generation(self) -> int:
        return int(self._recorder.requests)

    def handle(self, command: int, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Dispatch one command; returns (payload, flags)."""
        if command == Command.HELLO:
            return {
                "server": "wreath",
                "protocol": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "capabilities": self._capabilities(),
            }, 0
        if command == Command.WORKERS:
            return {
                "generation": self._generation(),
                "workers": [_worker_payload(self._recorder)],
            }, 0
        if command == Command.PRESSURE:
            worker = _worker_payload(self._recorder)
            worker["generation"] = self._generation()
            return worker, 0
        if command == Command.ACTIVE_REQUESTS:
            return self._active_requests(payload)
        if command == Command.EXPLAIN_ROUTE:
            return self._explain_route(payload), 0
        if command == Command.EXPLAIN_PLAN:
            return self._explain_plan(payload), 0
        if command == Command.METADATA:
            return self._metadata(payload)
        if command == Command.TIMELINE:
            return self._timeline(payload)
        if command == Command.RECENT_FAILURES:
            return self._recent_failures(payload)
        if command == Command.ROUTE_DISTRIBUTIONS:
            return self._route_distributions(payload), 0
        if command in _CAPTURE_COMMANDS:
            return self._capture_command(command, payload), 0
        raise InspectorError(f"unknown command: {command}")

    def _authorize_capture(self, payload: dict[str, Any]) -> None:
        """Gate a capture-control command on the capability token. The token
        requirement is separate from read-only access: a client that can read the
        Inspector still cannot arm capture without the secret."""
        if self._arm_registry is None or not self._config.capture_token:
            raise InspectorError("capture control is not enabled on this server")
        token = payload.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._config.capture_token):
            raise InspectorError("invalid or missing capture token")

    def _capture_command(self, command: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._authorize_capture(payload)
        if command == Command.CAPTURE_STATUS:
            return self._capture_status()
        if command == Command.ARM_CAPTURE:
            return self._arm_capture(payload)
        if command == Command.DISARM_CAPTURE:
            arm_id = payload.get("arm_id")
            if not isinstance(arm_id, int) or isinstance(arm_id, bool):
                raise InspectorError("DISARM_CAPTURE needs an integer arm_id")
            return {"disarmed": self._arm_registry.disarm(arm_id)}
        raise InspectorError(f"unknown capture command: {command}")

    def _arm_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .recording import RecordingPolicyError

        try:
            capture = _capture_policy_from_payload(payload)
            arm = self._arm_registry.arm(capture)
        except (RecordingPolicyError, ValueError) as error:
            # A rejected arm (over the ceiling, no expiry, too many) is a client
            # error, not an internal fault -- surface it as one error frame.
            raise InspectorError(str(error)) from error
        return {
            "arm_id": arm.arm_id,
            "expires_in": round(arm.expires_in, 3),
            "remaining_matches": arm.remaining_matches,
            "headers": sorted(arm.compiled.header_rules),
        }

    def _capture_status(self) -> dict[str, Any]:
        registry = self._arm_registry
        ceiling = registry.ceiling
        return {
            "ceiling": {
                "capture_slabs": ceiling.capture_slabs,
                "max_capture_bytes": ceiling.max_capture_bytes,
                "header_allowlist": sorted(ceiling.redaction.header_allowlist),
                "header_hash": sorted(ceiling.redaction.header_hash),
                "header_mask": sorted(ceiling.redaction.header_mask),
                "query_allowlist": sorted(ceiling.redaction.query_allowlist),
                "query_hash": sorted(ceiling.redaction.query_hash),
                "query_mask": sorted(ceiling.redaction.query_mask),
                "body": ceiling.redaction.body.value,
                "dependency": ceiling.redaction.dependency.value,
            },
            "arms": [
                {
                    "arm_id": arm.arm_id,
                    "expires_in": round(arm.expires_in, 3),
                    "remaining_matches": arm.remaining_matches,
                    "headers": sorted(arm.compiled.header_rules),
                }
                for arm in registry.active()
            ],
        }

    def _snapshot(self) -> Any:
        if self._projector is None:
            raise InspectorError("projection is not enabled on this server")
        return self._projector.snapshot()

    def _timeline(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        snapshot = self._snapshot()
        return self._paged_traces(payload, snapshot.recent, snapshot)

    def _recent_failures(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        snapshot = self._snapshot()
        return self._paged_traces(payload, snapshot.failures, snapshot)

    def _paged_traces(
        self, payload: dict[str, Any], rows: Any, snapshot: Any
    ) -> tuple[dict[str, Any], int]:
        offset = _page_int(payload, "offset", 0)
        limit = min(_page_int(payload, "limit", MAX_PAGE_ROWS), MAX_PAGE_ROWS)
        total = len(rows)
        stop = total - offset
        start = max(stop - limit, 0)
        page = list(reversed(rows[start:stop])) if stop > 0 else []
        truncated = offset + limit < total
        return {
            "generation": self._generation(),
            "assembled": snapshot.assembled,
            "total": total,
            "truncated": truncated,
            "loss": _projector_loss(snapshot.loss),
            "traces": [_trace_payload(trace) for trace in page],
        }, FLAG_TRUNCATED if truncated else 0

    def _route_distributions(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._snapshot()
        self._metadata_image()
        routes = cast(dict[int, Any], self._routes_by_id)
        distributions = []
        for metric in sorted(snapshot.routes, key=lambda m: m.count, reverse=True):
            route = routes.get(metric.route_id)
            distributions.append(
                {
                    "route_id": metric.route_id,
                    "method": route.method if route is not None else None,
                    "path": route.path if route is not None else None,
                    "count": metric.count,
                    "errors": metric.errors,
                    "duration_us_sum": metric.duration_us_sum,
                    "duration_us_max": metric.duration_us_max,
                    "buckets": list(metric.buckets),
                }
            )
        return {
            "generation": self._generation(),
            "assembled": snapshot.assembled,
            "loss": _projector_loss(snapshot.loss),
            "routes": distributions,
        }

    def _active_requests(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        offset = _page_int(payload, "offset", 0)
        limit = min(_page_int(payload, "limit", MAX_PAGE_ROWS), MAX_PAGE_ROWS)
        rows = self._recorder.active_snapshot()
        now_ns = time.monotonic_ns()
        page = rows[offset : offset + limit]
        truncated = offset + limit < len(rows)
        return {
            "generation": self._generation(),
            "total": len(rows),
            "truncated": truncated,
            "requests": [
                {
                    "request_id": request_id,
                    "age_us": max(0, (now_ns - start_ns) // 1000),
                    "protocol": _protocol_name(protocol),
                    "route_id": route_id,
                }
                for request_id, start_ns, protocol, route_id in page
            ],
        }, FLAG_TRUNCATED if truncated else 0

    def _explain_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._metadata_image()
        routes_by_id = cast(dict[int, Any], self._routes_by_id)
        routes_by_key = cast(dict[tuple[str, str], Any], self._routes_by_key)
        names = cast(dict[str, dict[int, str]], self._metadata_names)
        route_id = payload.get("route_id")
        if route_id is not None:
            try:
                route = routes_by_id.get(route_id)
            except TypeError:
                route = None
        else:
            method = payload.get("method")
            path = payload.get("path")
            if not isinstance(method, str) or not isinstance(path, str):
                raise InspectorError("EXPLAIN_ROUTE needs route_id or method+path")
            route = routes_by_key.get((method, path))
        if route is None:
            raise InspectorError("route not found")
        dependencies = names["dependencies"]
        middleware = names["middleware"]
        auth_policies = names["auth_policies"]
        return {
            "route_id": route.route_id,
            "method": route.method,
            "path": route.path,
            "operation_id": route.operation_id,
            "plan_id": route.plan_id,
            "tags": list(route.tags),
            "coverage": route.coverage,
            "dependencies": [
                _metadata_name(dependencies, identifier) for identifier in route.dependency_ids
            ],
            "middleware": [
                _metadata_name(middleware, identifier) for identifier in route.middleware_ids
            ],
            "auth_policy": auth_policies.get(route.auth_policy_id),
        }

    def _explain_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._metadata_image()
        plans_by_id = cast(dict[int, Any], self._plans_by_id)
        names = cast(dict[str, dict[int, str]], self._metadata_names)
        plan_id = payload.get("plan_id")
        try:
            plan = plans_by_id.get(plan_id)
        except TypeError:
            plan = None
        if plan is None:
            raise InspectorError("plan not found")
        serializers = names["serializers"]
        validators = names["validators"]
        limits = names["limits"]
        return {
            "plan_id": plan.plan_id,
            "params": list(plan.params),
            "body_type": plan.body_type,
            "returns_type": plan.returns_type,
            "serializer": serializers.get(plan.serializer_id),
            "validator": validators.get(plan.validator_id),
            "limits": [_metadata_name(limits, identifier) for identifier in plan.limit_ids],
        }

    def _metadata(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        table = payload.get("table")
        if table not in _METADATA_TABLES:
            raise InspectorError(f"unknown metadata table: {table!r}")
        image = self._metadata_image()
        records = getattr(image, table)
        offset = _page_int(payload, "offset", 0)
        limit = min(_page_int(payload, "limit", MAX_PAGE_ROWS), MAX_PAGE_ROWS)
        selected = records[offset : offset + limit]
        if table == "routes":
            rows = [
                {
                    "id": route.route_id,
                    "method": route.method,
                    "path": route.path,
                    "operation_id": route.operation_id,
                    "plan_id": route.plan_id,
                }
                for route in selected
            ]
        elif table == "plans":
            rows = [{"id": plan.plan_id, "params": list(plan.params)} for plan in selected]
        else:
            rows = [{"id": entry.entry_id, "name": entry.name} for entry in selected]
        total = len(records)
        truncated = offset + limit < total
        return {
            "table": table,
            "total": total,
            "truncated": truncated,
            "rows": rows,
        }, FLAG_TRUNCATED if truncated else 0


def _trace_payload(trace: Any) -> dict[str, Any]:
    """Serialize a projected trace for the wire. 128/64-bit correlation IDs go as
    hex strings (OTLP's form) so no JSON integer precision is assumed."""
    disposition = None
    if trace.flags & FLAG_POLICY_REFUSED:
        disposition = "ai_scraping" if trace.flags & FLAG_AI_SCRAPING_REFUSED else "refused"
    return {
        "request_id": trace.request_id,
        "connection_id": trace.connection_id,
        "route_id": trace.route_id,
        "plan_id": trace.plan_id,
        "worker_id": trace.worker_id,
        "duration_us": trace.duration_us,
        "status": trace.status,
        "terminal": trace.terminal.name.lower(),
        "protocol": _protocol_name(int(trace.protocol)),
        "error_class": trace.error_class,
        "flags": trace.flags,
        "policy_disposition": disposition,
        "bytes_in": trace.bytes_in,
        "bytes_out": trace.bytes_out,
        "is_failure": trace.is_failure,
        "trace_id": format(trace.trace_id, "032x") if trace.has_correlation else None,
        "span_id": format(trace.span_id, "016x") if trace.has_correlation else None,
        "observed_unix_nano": trace.observed_unix_nano,
        "phases": [
            {
                "phase": phase.phase_id.name.lower(),
                "coverage": phase.coverage.name.lower(),
                "dependency_id": phase.dependency_id,
                "start_offset_us": phase.start_offset_us,
                "duration_us": phase.duration_us,
                "sequence": phase.sequence,
            }
            for phase in trace.phases
        ],
    }


def _projector_loss(loss: Any) -> dict[str, int]:
    return {
        "orphan_phase": loss.orphan_phase,
        "orphan_correlation": loss.orphan_correlation,
        "pending_evicted": loss.pending_evicted,
        "decode_error": loss.decode_error,
        "export_error": loss.export_error,
        "recent_evicted": loss.recent_evicted,
    }


def _page_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InspectorError(f"{key} must be a non-negative integer")
    return value


def _str_set(payload: dict[str, Any], key: str) -> frozenset[str]:
    value: Any = payload.get(key, [])
    if not isinstance(value, list):
        raise InspectorError(f"{key} must be a list of strings")
    for entry in value:
        if not isinstance(entry, str):
            raise InspectorError(f"{key} must be a list of strings")
    return frozenset(cast(list[str], value))


def _capture_policy_from_payload(payload: dict[str, Any]) -> Any:
    """Build a CapturePolicy from an ARM_CAPTURE payload. Validation lives in the
    recording value types (they raise RecordingPolicyError), so a malformed or
    over-ceiling arm becomes one error frame rather than a partial install."""
    from .recording import (
        BodyCapture,
        CaptureBudget,
        CapturePolicy,
        RedactionPolicy,
    )

    redaction = payload.get("redaction", {})
    if not isinstance(redaction, dict):
        raise InspectorError("redaction must be an object")
    budget = payload.get("budget", {})
    if not isinstance(budget, dict):
        raise InspectorError("budget must be an object")
    body_value = redaction.get("body", BodyCapture.METADATA.value)
    try:
        body = BodyCapture(body_value)
    except ValueError as exc:
        raise InspectorError(f"unknown body capture mode {body_value!r}") from exc
    dependency_value = redaction.get("dependency", BodyCapture.NONE.value)
    try:
        dependency = BodyCapture(dependency_value)
    except ValueError as exc:
        raise InspectorError(f"unknown dependency capture mode {dependency_value!r}") from exc
    return CapturePolicy(
        redaction=RedactionPolicy(
            header_allowlist=_str_set(redaction, "header_allowlist"),
            header_hash=_str_set(redaction, "header_hash"),
            header_mask=_str_set(redaction, "header_mask"),
            query_allowlist=_str_set(redaction, "query_allowlist"),
            query_hash=_str_set(redaction, "query_hash"),
            query_mask=_str_set(redaction, "query_mask"),
            body=body,
            dependency=dependency,
            max_body_bytes=_page_int(redaction, "max_body_bytes", 0),
            max_fields=_page_int(redaction, "max_fields", 0),
            max_depth=_page_int(redaction, "max_depth", 0),
        ),
        budget=CaptureBudget(
            slabs=_page_int(budget, "slabs", 0),
            slab_bytes=_page_int(budget, "slab_bytes", 64 * 1024),
            per_request_bytes=_page_int(budget, "per_request_bytes", 0),
            per_route_bytes=_page_int(budget, "per_route_bytes", 0),
        ),
        expiry_seconds=float(payload.get("expiry_seconds", 0.0) or 0.0),
        max_matches=_page_int(payload, "max_matches", 0),
    )


class _InspectorProtocol(asyncio.Protocol):
    """One client connection: strict framing, one command in flight."""

    def __init__(self, server: InspectorServer) -> None:
        self._server = server
        self._transport: asyncio.Transport | None = None
        self._buffer = bytearray()
        self._idle_handle: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not _peer_authorized(transport):
            transport.close()
            return
        if not isinstance(transport, asyncio.Transport):
            transport.close()
            return
        self._transport = transport
        self._reset_idle_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        self._transport = None

    def _reset_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(self._server._config.idle_timeout, self._idle_expired)

    def _idle_expired(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def data_received(self, data: bytes) -> None:
        if self._transport is None:
            return
        self._buffer += data
        self._reset_idle_timer()
        while True:
            if len(self._buffer) < HEADER.size:
                # Garbage cannot hide in a short prefix: check what we have.
                if len(self._buffer) >= 4 and bytes(self._buffer[:4]) != MAGIC:
                    self._fail(0, "bad magic")
                    return
                return
            magic, version, command, _flags, request_id, length = HEADER.unpack(
                bytes(self._buffer[: HEADER.size])
            )
            if magic != MAGIC:
                self._fail(request_id, "bad magic")
                return
            if version != PROTOCOL_VERSION:
                self._fail(request_id, f"unsupported protocol version {version}")
                return
            if length > self._server._config.max_payload_bytes:
                self._fail(request_id, "payload too large")
                return
            if len(self._buffer) < HEADER.size + length:
                return
            raw = bytes(self._buffer[HEADER.size : HEADER.size + length])
            del self._buffer[: HEADER.size + length]
            self._dispatch(command, request_id, raw)
            if self._transport is None:
                return

    def _dispatch(self, command: int, request_id: int, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise InspectorError("payload must be a JSON object")
            body, flags = self._server.handle(command, payload)
        except InspectorError as error:
            self._send(command, FLAG_ERROR, request_id, {"error": str(error)})
            return
        except Exception as error:  # noqa: BLE001 - command dispatch; reported, not swallowed
            # `self._server.handle` dispatches to a command implementation. A
            # bug in one must not take down the process it is inspecting, and
            # the failure is *sent back to the caller* rather than dropped --
            # the error reaches a human either way, which is what separates
            # this from a silent catch.
            self._send(command, FLAG_ERROR, request_id, {"error": f"internal: {error}"})
            return
        self._send(command, flags, request_id, body)

    def _send(self, command: int, flags: int, request_id: int, body: dict[str, Any]) -> None:
        transport = self._transport
        if transport is None:
            return
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        header = HEADER.pack(MAGIC, PROTOCOL_VERSION, command, flags, request_id, len(payload))
        transport.write(header + payload)

    def _fail(self, request_id: int, message: str) -> None:
        # One error frame, then the connection closes: a malformed client
        # cannot hold a parser in a bad state.
        self._send(0, FLAG_ERROR, request_id, {"error": message})
        if self._transport is not None:
            self._transport.close()
            self._transport = None


def _peer_authorized(transport: asyncio.BaseTransport) -> bool:
    """Same-UID (or root) peers only, where the platform can tell us."""
    sock = transport.get_extra_info("socket")
    if sock is None:
        return False
    if hasattr(socket, "SO_PEERCRED"):
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
        except OSError:
            return False
        return uid in (os.getuid(), 0)
    # No credential passing on this platform: the 0600 socket is the gate.
    return True


class InspectorClient:
    """A small protocol client for the CLI and tests."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0

    async def __aenter__(self) -> InspectorClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(self._path)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except ConnectionError, OSError:
                pass
            self._writer = None
            self._reader = None

    async def call(
        self, command: Command | int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._reader is None or self._writer is None:
            raise InspectorError("client is not connected")
        self._request_id += 1
        request_id = self._request_id
        raw = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
        header = HEADER.pack(MAGIC, PROTOCOL_VERSION, int(command), 0, request_id, len(raw))
        self._writer.write(header + raw)
        await self._writer.drain()
        reply = await self._reader.readexactly(HEADER.size)
        magic, version, _command, flags, echoed, length = HEADER.unpack(reply)
        if magic != MAGIC or version != PROTOCOL_VERSION:
            raise InspectorError("malformed response header")
        if length > MAX_PAYLOAD_BYTES:
            raise InspectorError("oversized response")
        body = json.loads(await self._reader.readexactly(length)) if length else {}
        if echoed != request_id:
            raise InspectorError("response id mismatch")
        if flags & FLAG_ERROR:
            raise InspectorError(body.get("error", "unknown inspector error"))
        if flags & FLAG_TRUNCATED:
            body["truncated"] = True
        return body

    async def hello(self) -> dict[str, Any]:
        return await self.call(Command.HELLO)

    async def workers(self) -> list[WorkerSnapshot]:
        body = await self.call(Command.WORKERS)
        return [
            WorkerSnapshot(
                mode=worker["mode"],
                requests=worker["requests"],
                completions=worker["completions"],
                active_count=worker["active_count"],
                ring_occupancy=worker["ring_occupancy"],
                ring_high_water=worker["ring_high_water"],
                phase_capacity=worker["phase_capacity"],
                phase_in_use=worker["phase_in_use"],
                phase_high_water=worker["phase_high_water"],
                losses=worker["losses"],
            )
            for worker in body["workers"]
        ]

    async def active_requests(
        self, *, offset: int = 0, limit: int = MAX_PAGE_ROWS
    ) -> list[ActiveRequest]:
        body = await self.call(Command.ACTIVE_REQUESTS, {"offset": offset, "limit": limit})
        return [
            ActiveRequest(
                request_id=request["request_id"],
                age_us=request["age_us"],
                protocol=request["protocol"],
                route_id=request["route_id"],
            )
            for request in body["requests"]
        ]

    async def pressure(self) -> dict[str, Any]:
        return await self.call(Command.PRESSURE)

    async def explain_route(
        self,
        *,
        route_id: int | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if route_id is not None:
            payload["route_id"] = route_id
        if method is not None:
            payload["method"] = method
        if path is not None:
            payload["path"] = path
        return await self.call(Command.EXPLAIN_ROUTE, payload)

    async def explain_plan(self, plan_id: int) -> dict[str, Any]:
        return await self.call(Command.EXPLAIN_PLAN, {"plan_id": plan_id})

    async def metadata(
        self, table: str, *, offset: int = 0, limit: int = MAX_PAGE_ROWS
    ) -> dict[str, Any]:
        return await self.call(Command.METADATA, {"table": table, "offset": offset, "limit": limit})

    async def timeline(self, *, offset: int = 0, limit: int = MAX_PAGE_ROWS) -> dict[str, Any]:
        return await self.call(Command.TIMELINE, {"offset": offset, "limit": limit})

    async def recent_failures(
        self, *, offset: int = 0, limit: int = MAX_PAGE_ROWS
    ) -> dict[str, Any]:
        return await self.call(Command.RECENT_FAILURES, {"offset": offset, "limit": limit})

    async def route_distributions(self) -> dict[str, Any]:
        return await self.call(Command.ROUTE_DISTRIBUTIONS)

    async def arm_capture(
        self,
        *,
        token: str,
        redaction: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        expiry_seconds: float,
        max_matches: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": token,
            "expiry_seconds": expiry_seconds,
            "max_matches": max_matches,
        }
        if redaction is not None:
            payload["redaction"] = redaction
        if budget is not None:
            payload["budget"] = budget
        return await self.call(Command.ARM_CAPTURE, payload)

    async def disarm_capture(self, *, token: str, arm_id: int) -> dict[str, Any]:
        return await self.call(Command.DISARM_CAPTURE, {"token": token, "arm_id": arm_id})

    async def capture_status(self, *, token: str) -> dict[str, Any]:
        return await self.call(Command.CAPTURE_STATUS, {"token": token})


async def serve_inspector(
    recorder: Any,
    app: Any,
    config: InspectorConfig,
    projector: Any = None,
    arm_registry: Any = None,
) -> InspectorServer:
    """Start the read-only Inspector beside `recorder` and return the server.

    When `projector` is given, the projection-backed commands (TIMELINE,
    RECENT_FAILURES, ROUTE_DISTRIBUTIONS) become available. When `arm_registry`
    is given *and* the config carries a `capture_token`, the capture-control
    commands (ARM_CAPTURE, DISARM_CAPTURE, CAPTURE_STATUS) become available behind
    that token.
    """
    server = InspectorServer(recorder, app, config, projector=projector, arm_registry=arm_registry)
    await server.start()
    return server


__all__ = [
    "ActiveRequest",
    "Command",
    "InspectorClient",
    "InspectorConfig",
    "InspectorError",
    "InspectorServer",
    "WorkerSnapshot",
    "serve_inspector",
]
