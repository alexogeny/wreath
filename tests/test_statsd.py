from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys
import types

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "wreath"


def _mod(name: str):
    try:
        return importlib.import_module(f"wreath.{name}")
    except ImportError:
        pkg = sys.modules.get("wreath")
        if not isinstance(pkg, types.ModuleType) or not getattr(pkg, "__path__", None):
            pkg = types.ModuleType("wreath")
            pkg.__path__ = [str(_SRC)]
            sys.modules["wreath"] = pkg
        for dep in ("_prometheus", name):
            key = f"wreath.{dep}"
            if key not in sys.modules:
                spec = importlib.util.spec_from_file_location(key, _SRC / f"{dep}.py")
                m = importlib.util.module_from_spec(spec)
                sys.modules[key] = m
                spec.loader.exec_module(m)
        return sys.modules[f"wreath.{name}"]


statsd = _mod("_statsd")


class _Loss:
    _FIELDS = (
        "orphan_phase",
        "orphan_correlation",
        "pending_evicted",
        "decode_error",
        "export_error",
        "recent_evicted",
    )

    def __init__(self, **kw):
        for f in self._FIELDS:
            setattr(self, f, int(kw.get(f, 0)))


class _Route:
    def __init__(self, route_id, count, errors, dsum, dmax, buckets=None):
        self.route_id = route_id
        self.count = count
        self.errors = errors
        self.duration_us_sum = dsum
        self.duration_us_max = dmax
        self.buckets = buckets or [0] * 64


class _Snap:
    def __init__(self, assembled, pending, routes, loss):
        self.assembled = assembled
        self.pending = pending
        self.routes = routes
        self.loss = loss


class _Reason:
    def __init__(self, name):
        self.name = name


def _parse(lines):
    """{metric_name: (value, kind, tags_or_None)} for one flush's lines."""
    out = {}
    for ln in lines:
        head, _, kind = ln.partition("|")
        kind, _, tagpart = kind.partition("|#")
        name, _, val = head.partition(":")
        out[name] = (val, kind, tagpart or None)
    return out


def test_plain_statsd_folds_labels_and_deltas():
    b = statsd.StatsDBridge(_Src(_Snap(0, 0, [], _Loss())), prefix="wreath")
    snap = _Snap(
        assembled=10,
        pending=3,
        routes=[_Route(7, count=5, errors=1, dsum=4000.0, dmax=900.0)],
        loss=_Loss(decode_error=2),
    )
    p = _parse(b._lines(snap))
    # counter deltas (first flush → full value); gauges absolute; labels folded into name.
    assert p["wreath.http.requests.7"] == ("5", "c", None)
    assert p["wreath.http.errors.7"] == ("1", "c", None)
    assert p["wreath.http.duration.sum_ms.7"][1] == "c"  # 4000us/1000 = 4.0 → "4"
    assert p["wreath.http.duration.max_ms.7"] == ("0.9", "g", None)
    assert p["wreath.flight.assembled"] == ("10", "c", None)
    assert p["wreath.flight.pending"] == ("3", "g", None)
    assert p["wreath.flight.projector_loss.decode_error"] == ("2", "c", None)
    # second flush: only the increment is sent for counters; gauge re-sent absolute.
    snap2 = _Snap(
        assembled=14,
        pending=1,
        routes=[_Route(7, count=8, errors=1, dsum=4000.0, dmax=100.0)],
        loss=_Loss(decode_error=2),
    )
    p2 = _parse(b._lines(snap2))
    assert p2["wreath.http.requests.7"] == ("3", "c", None)  # 8-5
    assert p2["wreath.http.errors.7"] == ("0", "c", None)  # 1-1
    assert p2["wreath.flight.assembled"] == ("4", "c", None)  # 14-10
    assert p2["wreath.flight.pending"] == ("1", "g", None)
    assert p2["wreath.flight.projector_loss.decode_error"] == ("0", "c", None)


def test_dogstatsd_tags():
    b = statsd.StatsDBridge(_Src(_Snap(0, 0, [], _Loss())), dogstatsd=True, tags={"env": "prod"})
    snap = _Snap(1, 0, [_Route(7, 5, 0, 0.0, 0.0)], _Loss(export_error=3))
    lines = b._lines(snap)
    # per-route counter: value on the metric, labels+static tags in `|#...`.
    req = next(ln for ln in lines if ln.startswith("wreath.http.requests:"))
    assert req.startswith("wreath.http.requests:5|c|#")
    assert "route_id:7" in req and "env:prod" in req
    # loss is one line per reason, all sharing the metric name — differ by tag.
    loss = next(
        ln
        for ln in lines
        if ln.startswith("wreath.flight.projector_loss:") and "reason:export_error" in ln
    )
    assert loss.startswith("wreath.flight.projector_loss:3|c|#") and "env:prod" in loss


def test_route_label_resolvers_and_tag_sanitization():
    snap = _Snap(1, 0, [_Route(7, 5, 0, 0.0, 0.0)], _Loss())
    mapping = {7: {"route:name": "café west", "env": "route"}}
    b = statsd.StatsDBridge(
        _Src(snap),
        prefix="my service",
        dogstatsd=True,
        tags={"env": "static", "region": "ap:south"},
        route_labels=mapping,
    )

    request = next(line for line in b._lines(snap) if ".http.requests:" in line)
    assert request.startswith("my_service.http.requests:5|c|#")
    assert request.endswith("env:route,region:ap_south,route_name:café_west")

    callable_bridge = statsd.StatsDBridge(
        _Src(snap), route_labels=lambda route_id: {"endpoint": f"route {route_id}"}
    )
    request = next(line for line in callable_bridge._lines(snap) if ".http.requests." in line)
    assert request == "wreath.http.requests.route_7:5|c"


def test_recorder_loss_reason_and_counter_reset():
    b = statsd.StatsDBridge(_Src(_Snap(0, 0, [], _Loss())), dogstatsd=True)
    snap = _Snap(0, 0, [], _Loss())
    rec = {_Reason("RING_FULL"): 4}
    p = _parse(b._lines(snap, rec))
    assert p["wreath.flight.recorder_loss"][0] == "4"
    assert "reason:ring_full" in p["wreath.flight.recorder_loss"][2]
    # a counter that goes backwards (reset) sends the current value, never negative.
    p2 = _parse(b._lines(snap, {_Reason("RING_FULL"): 1}))
    assert p2["wreath.flight.recorder_loss"][0] == "1"


def test_flush_sends_udp_packets():
    sent = []

    class _FakeSock:
        def setblocking(self, _):
            pass

        def sendto(self, data, addr):
            sent.append((data, addr))

        def close(self):
            pass

    snap = _Snap(1, 0, [_Route(i, 1, 0, 0.0, 0.0) for i in range(50)], _Loss())
    b = statsd.StatsDBridge(_Src(snap), host="10.0.0.1", port=9999)
    b._sock = _FakeSock()
    n = b.flush()
    assert n > 0 and sent
    assert all(addr == ("10.0.0.1", 9999) for _, addr in sent)
    # every datagram is under the MTU cap and newline-joined.
    assert all(len(data) <= statsd.MAX_PACKET_BYTES for data, _ in sent)
    joined = b"\n".join(d for d, _ in sent)
    assert b"wreath.http.requests.0:1|c" in joined


def test_native_packets_match_the_independent_line_packetizer():
    snap = _Snap(3, 1, [_Route(i, i + 1, 0, 1250.0, 25.0) for i in range(5)], _Loss())
    line_bridge = statsd.StatsDBridge(
        _Src(snap), prefix="my service", dogstatsd=True, tags={"env": "prod"}
    )
    lines = line_bridge._lines(snap, {})
    lines.append("my_service.jobs.run_errors:7|g|#env:prod,instance:work")

    expected = []
    packet = []
    size = 0
    maximum = 90
    for line in lines:
        added = len(line.encode("utf-8")) + 1
        if packet and size + added > maximum:
            expected.append("\n".join(packet).encode("utf-8"))
            packet, size = [], 0
        packet.append(line)
        size += added
    if packet:
        expected.append("\n".join(packet).encode("utf-8"))

    packet_bridge = statsd.StatsDBridge(
        _Src(snap), prefix="my service", dogstatsd=True, tags={"env": "prod"}
    )
    reading = types.SimpleNamespace(
        subsystem="jobs",
        instance="work",
        values=types.MappingProxyType({"run_errors": 7}),
    )
    packets, count = statsd._core.statsd_packets(
        snap,
        {},
        packet_bridge._prefix,
        packet_bridge._dogstatsd,
        packet_bridge._tags,
        packet_bridge._route_labels,
        packet_bridge._deltas,
        (reading,),
        maximum,
    )
    assert packets == tuple(expected)
    assert count == len(lines)


class _Src:
    def __init__(self, snap, rec=None):
        self._snap = snap
        self._rec = rec

    def snapshot(self):
        return self._snap

    def recorder_loss(self):
        return self._rec or {}


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_"):
            _f()
            print(f"ok  {_n}")
    print("statsd tests passed")
