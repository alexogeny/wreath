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

from ._native import _core
from ._prometheus import RouteLabels

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
        "_source",
        "_addr",
        "_prefix",
        "_dogstatsd",
        "_tags",
        "_route_labels",
        "_sock",
        "_deltas",
        "_app",
        "_counter_sources",
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
        counter_sources: tuple[Any, ...] = (),
    ) -> None:
        from .metrics import _counter_sources, _snapshot_source

        explicit_sources = _counter_sources(counter_sources, bridge="StatsD")
        #: Asked for subsystem counters on every flush. Optional, as on the
        #: Prometheus bridge and for the same reason.
        self._app = app
        self._counter_sources = explicit_sources
        self._source = _snapshot_source(source, bridge="statsd")
        self._addr = (host, port)
        self._prefix = prefix.rstrip(".")
        self._dogstatsd = dogstatsd
        self._tags = dict(tags or {})
        self._route_labels = route_labels
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._deltas = _core.metric_delta_state()

    def _emit(
        self, out: list[str], name: str, value: float, kind: str, labels: dict[str, str]
    ) -> None:
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
        return _core.statsd_lines(
            snapshot,
            recorder_loss,
            self._prefix,
            self._dogstatsd,
            self._tags,
            self._route_labels,
            self._deltas,
        )

    def _counter_lines(self, out: list[str]) -> None:
        """Every registered subsystem's counters, as gauges.

        Gauges rather than counters, and deliberately *not* delta-tracked like
        the route aggregates above. Canonical subsystem rows have historically
        been current readings in StatsD; preserving that shape avoids changing
        an existing series from a gauge into an increment stream. CloudWatch's
        separately documented SUM contract uses `Counters.gauges` to distinguish
        the values that must remain absolute.
        """
        from .metrics import collect

        for reading in collect(self._app, self._counter_sources):
            for name, value in reading.values.items():
                self._emit(
                    out,
                    f"{reading.subsystem}.{name}",
                    int(value),
                    "g",
                    {"instance": str(reading.instance)},
                )

    def flush(self) -> int:
        """Read one snapshot, send its metrics; returns the line count."""
        from .metrics import _read_snapshot, collect

        snapshot, recorder_loss = _read_snapshot(self._source)
        packets, line_count = _core.statsd_packets(
            snapshot,
            recorder_loss,
            self._prefix,
            self._dogstatsd,
            self._tags,
            self._route_labels,
            self._deltas,
            collect(self._app, self._counter_sources),
            MAX_PACKET_BYTES,
        )
        for packet in packets:
            self._send_datagram(packet)
        return line_count

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
        self._send_datagram("\n".join(packet).encode("utf-8"))

    def _send_datagram(self, packet: bytes) -> None:
        try:
            self._sock.sendto(packet, self._addr)
        except OSError:
            pass  # telemetry never breaks the app

    async def run_periodic(self, interval: float = 10.0) -> None:
        """Flush on a fixed cadence."""
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
    app: Any = None,
    counter_sources: tuple[Any, ...] = (),
) -> StatsDBridge:
    """Wrap a snapshot source in a StatsD/DogStatsD push bridge (see module doc)."""
    return StatsDBridge(
        source,
        host=host,
        port=port,
        prefix=prefix,
        dogstatsd=dogstatsd,
        tags=tags,
        route_labels=route_labels,
        app=app,
        counter_sources=counter_sources,
    )
