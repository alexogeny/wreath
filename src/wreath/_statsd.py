"""StatsD / DogStatsD push bridge for the Native Flight Recorder.

Where `wreath._prometheus` exposes the projector's aggregates for *scrape*,
this bridge *pushes* the same `Projector.snapshot()` state as StatsD lines over
UDP. Counters are sent as **deltas** since the previous flush (StatsD aggregates
increments); gauges are sent absolute. Because the recorder aggregates off-path,
per-route durations are emitted as sum/max aggregates (`|c`/`|g`), not raw
`|ms` samples we do not retain.

Plain StatsD folds labels into the metric name (`wreath.http.requests.7`);
DogStatsD keeps them as `|#route_id:7` tags. Zero-dependency: UDP via stdlib
`socket`; sends are non-blocking and errors are swallowed (telemetry must never
break the app).
"""

from __future__ import annotations

import re
import socket
from typing import Any

from ._metricdelta import DeltaTracker
from ._prometheus import (
    _PROJECTOR_LOSS_FIELDS,
    RouteLabels,
    _loss_reason_name,
    _resolve_route_labels,
)

__all__ = ["StatsDBridge", "activate_statsd"]

#: Keep UDP payloads under a conservative MTU; multiple metrics per packet are
#: newline-separated (StatsD multi-metric packet form).
MAX_PACKET_BYTES = 1400

_NAME_INVALID = re.compile(r"[^a-zA-Z0-9._-]")
_TAG_INVALID = re.compile(r"[,|#:\s]")


def _san(name: str) -> str:
    return _NAME_INVALID.sub("_", name)


def _san_tag(value: str) -> str:
    return _TAG_INVALID.sub("_", value)


def _fmt(value: float | int) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(int(value))


class StatsDBridge:
    """Pushes projector metrics to a StatsD/DogStatsD agent over UDP.

    `source` is a `wreath._projector.Projector` (anything with
    `snapshot()` and optionally `recorder_loss()`). Call `flush`
    periodically (or drive `run_periodic` from a supervised task).
    """

    __slots__ = (
        "_source", "_addr", "_prefix", "_dogstatsd", "_tags", "_route_labels",
        "_sock", "_deltas", "_app",
    )

    def __init__(
        self,
        source: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8125,
        prefix: str = "wreath",
        dogstatsd: bool = False,
        tags: dict[str, str] | None = None,
        route_labels: RouteLabels = None,
        app: Any = None,
    ) -> None:
        if not hasattr(source, "snapshot"):
            raise TypeError("statsd source must expose snapshot()")
        #: Asked for subsystem counters on every flush. Optional, as on the
        #: Prometheus bridge and for the same reason.
        self._app = app
        self._source = source
        self._addr = (host, port)
        self._prefix = prefix.rstrip(".")
        self._dogstatsd = dogstatsd
        self._tags = dict(tags or {})
        self._route_labels = route_labels
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._deltas = DeltaTracker()

    # -- delta bookkeeping (StatsD counters are increments) ------------------
    def _delta(self, key: tuple, value: float) -> float:
        return self._deltas.delta(key, value)

    # -- line building (pure; testable without a socket) ---------------------
    def _emit(self, out: list[str], name: str, value: float, kind: str,
              labels: dict[str, str]) -> None:
        metric = f"{self._prefix}.{name}"
        if self._dogstatsd:
            merged = {**self._tags, **labels}
            suffix = ""
            if merged:
                suffix = "|#" + ",".join(
                    f"{_san_tag(k)}:{_san_tag(str(v))}" for k, v in merged.items()
                )
            out.append(f"{_san(metric)}:{_fmt(value)}|{kind}{suffix}")
        else:
            parts = [_san(str(v)) for v in labels.values()]
            full = ".".join([_san(metric), *parts]) if parts else _san(metric)
            out.append(f"{full}:{_fmt(value)}|{kind}")

    def _lines(self, snapshot: Any, recorder_loss: dict | None = None) -> list[str]:
        out: list[str] = []
        for r in tuple(getattr(snapshot, "routes", ()) or ()):
            lbl = _resolve_route_labels(self._route_labels, r.route_id)
            self._emit(out, "http.requests",
                       self._delta(("req", r.route_id), int(r.count)), "c", lbl)
            self._emit(out, "http.errors",
                       self._delta(("err", r.route_id), int(r.errors)), "c", lbl)
            self._emit(out, "http.duration.sum_ms",
                       self._delta(("dsum", r.route_id), r.duration_us_sum / 1000.0),
                       "c", lbl)
            self._emit(out, "http.duration.max_ms",
                       r.duration_us_max / 1000.0, "g", lbl)
        self._emit(out, "flight.assembled",
                   self._delta(("assembled",), int(getattr(snapshot, "assembled", 0))),
                   "c", {})
        self._emit(out, "flight.pending",
                   int(getattr(snapshot, "pending", 0)), "g", {})
        loss = getattr(snapshot, "loss", None)
        for field in _PROJECTOR_LOSS_FIELDS:
            self._emit(out, "flight.projector_loss",
                       self._delta(("ploss", field), int(getattr(loss, field, 0))),
                       "c", {"reason": field})
        if recorder_loss:
            for reason, count in recorder_loss.items():
                name = _loss_reason_name(reason)
                self._emit(out, "flight.recorder_loss",
                           self._delta(("rloss", name), int(count)),
                           "c", {"reason": name})
        return out

    # -- sending -------------------------------------------------------------
    def _counter_lines(self, out: list[str]) -> None:
        """Every registered subsystem's counters, as gauges.

        Gauges rather than counters, and deliberately *not* delta-tracked like
        the route aggregates above: this bridge cannot tell a monotonic counter
        from a value that moves both ways, and sending a decrease as an
        increment would make a falling gauge read as a negative rate. A gauge is
        the reading that is true either way.
        """
        if self._app is None:
            return
        from .metrics import collect

        for reading in collect(self._app):
            for name, value in reading.values.items():
                self._emit(
                    out, f"{reading.subsystem}.{name}", int(value), "g",
                    {"instance": str(reading.instance)},
                )

    def flush(self) -> int:
        """Read one snapshot, send its metrics; returns the line count."""
        snapshot = self._source.snapshot()
        recorder_loss = None
        getter = getattr(self._source, "recorder_loss", None)
        if callable(getter):
            recorder_loss = getter()
        lines = self._lines(snapshot, recorder_loss)
        self._counter_lines(lines)
        self._send(lines)
        return len(lines)

    def _send(self, lines: list[str]) -> None:
        packet: list[str] = []
        size = 0
        for line in lines:
            n = len(line.encode("utf-8")) + 1
            if packet and size + n > MAX_PACKET_BYTES:
                self._send_packet(packet)
                packet, size = [], 0
            packet.append(line)
            size += n
        if packet:
            self._send_packet(packet)

    def _send_packet(self, packet: list[str]) -> None:
        try:
            self._sock.sendto("\n".join(packet).encode("utf-8"), self._addr)
        except OSError:
            pass  # telemetry never breaks the app

    async def run_periodic(self, interval: float = 10.0) -> None:
        """Flush on a fixed cadence. Drive from a supervised task.

        TODO(app-wiring): an `app.statsd(...)` factory that owns this loop in
        the lifespan would mirror `app.http_client`/`app.objects`.
        """
        import asyncio

        while True:
            self.flush()
            await asyncio.sleep(interval)

    def close(self) -> None:
        self._sock.close()


def activate_statsd(
    source: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8125,
    prefix: str = "wreath",
    dogstatsd: bool = False,
    tags: dict[str, str] | None = None,
    route_labels: RouteLabels = None,
) -> StatsDBridge:
    """Wrap a snapshot source in a StatsD/DogStatsD push bridge (see module doc)."""
    return StatsDBridge(
        source, host=host, port=port, prefix=prefix,
        dogstatsd=dogstatsd, tags=tags, route_labels=route_labels,
    )
