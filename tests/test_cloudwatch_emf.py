from __future__ import annotations

import importlib
import importlib.util
import json
import pathlib
import sys
import types

from wreath.metrics import Counters

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
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"cannot load test module {key}") from None
                m = importlib.util.module_from_spec(spec)
                sys.modules[key] = m
                spec.loader.exec_module(m)
        return sys.modules[f"wreath.{name}"]


emf = _mod("_cloudwatch_emf")


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
    def __init__(self, route_id, count, errors, dsum, dmax):
        self.route_id = route_id
        self.count = count
        self.errors = errors
        self.duration_us_sum = dsum
        self.duration_us_max = dmax
        self.buckets = [0] * 64


class _Snap:
    def __init__(self, assembled, pending, routes, loss):
        self.assembled = assembled
        self.pending = pending
        self.routes = routes
        self.loss = loss


class _Src:
    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return self._snap


def _metric_names(blob):
    return {m["Name"]: m["Unit"] for m in blob["_aws"]["CloudWatchMetrics"][0]["Metrics"]}


def test_route_and_global_blobs_shape():
    snap = _Snap(
        assembled=10,
        pending=2,
        routes=[_Route(7, count=5, errors=1, dsum=4000.0, dmax=900.0)],
        loss=_Loss(decode_error=3),
    )
    b = emf.EmfBridge(_Src(snap), namespace="Trailhead", dimensions={"Service": "api"})
    blobs = b.blobs(snap, timestamp_ms=1710000000000)
    assert len(blobs) == 2  # one route + one global
    route, glob = blobs

    # route blob: static + route dimensions, values at root, units correct.
    cw = route["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "Trailhead"
    assert cw["Dimensions"] == [["Service", "route_id"]]
    assert route["Service"] == "api" and route["route_id"] == "7"
    names = _metric_names(route)
    assert names["Requests"] == "Count" and names["Errors"] == "Count"
    assert names["DurationSum"] == "Milliseconds" and names["DurationMax"] == "Milliseconds"
    assert route["Requests"] == 5 and route["Errors"] == 1
    assert route["DurationSum"] == 4.0 and route["DurationMax"] == 0.9
    assert route["_aws"]["Timestamp"] == 1710000000000

    # global blob: assembled/pending + per-reason loss.
    gnames = _metric_names(glob)
    assert gnames["TracesAssembled"] == "Count" and gnames["Pending"] == "Count"
    assert glob["TracesAssembled"] == 10 and glob["Pending"] == 2
    assert glob["ProjectorLoss_decode_error"] == 3
    assert glob["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Service"]]


def test_counters_are_deltas_by_default():
    b = emf.EmfBridge(_Src(_Snap(0, 0, [], _Loss())))
    snap1 = _Snap(10, 0, [_Route(1, 5, 0, 0.0, 0.0)], _Loss())
    snap2 = _Snap(14, 0, [_Route(1, 9, 0, 0.0, 0.0)], _Loss())
    b.blobs(snap1, timestamp_ms=1)
    second = b.blobs(snap2, timestamp_ms=2)
    route = second[0]
    assert route["Requests"] == 4  # 9 - 5
    assert second[1]["TracesAssembled"] == 4  # 14 - 10


def test_cumulative_mode():
    b = emf.EmfBridge(_Src(_Snap(0, 0, [], _Loss())), cumulative=True)
    snap = _Snap(10, 0, [_Route(1, 5, 0, 0.0, 0.0)], _Loss())
    b.blobs(snap, timestamp_ms=1)
    again = b.blobs(snap, timestamp_ms=2)
    assert again[0]["Requests"] == 5  # absolute, not a delta


def test_route_dimension_resolvers_override_static_dimensions():
    snap = _Snap(1, 0, [_Route(3, 2, 0, 500.0, 250.0)], _Loss())
    bridge = emf.EmfBridge(
        _Src(snap),
        dimensions={"Service": "api", "Region": "static"},
        route_labels={3: {"Region": "route", "Endpoint": 17}},
    )

    route = bridge.blobs(snap, timestamp_ms=99)[0]

    assert route["Region"] == "route"
    assert route["Endpoint"] == "17"
    assert route["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
        ["Service", "Region", "Endpoint"]
    ]


def test_recorder_loss_names_reset_and_obey_the_metric_cap():
    class Reason:
        def __init__(self, name):
            self.name = name

    snap = _Snap(0, 0, [], _Loss())
    bridge = emf.EmfBridge(_Src(snap))
    reasons = {Reason(f"REASON_{index}"): index for index in range(110)}

    first = bridge.blobs(snap, timestamp_ms=1, recorder_loss=reasons)[0]
    definitions = first["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    assert len(definitions) == emf._MAX_METRICS_PER_BLOB
    assert first["RecorderLoss_reason_1"] == 1

    reason = next(item for item in reasons if item.name == "REASON_1")
    second = bridge.blobs(snap, timestamp_ms=2, recorder_loss={reason: 0})[0]
    assert second["RecorderLoss_reason_1"] == 0


def test_render_emits_valid_json_lines():
    snap = _Snap(1, 0, [_Route(3, 2, 0, 500.0, 250.0)], _Loss())
    text = emf.EmfBridge(_Src(snap)).render(timestamp_ms=99)
    lines = text.splitlines()
    assert len(lines) == 2
    for ln in lines:
        obj = json.loads(ln)  # each line is a standalone EMF document
        assert "_aws" in obj and obj["_aws"]["Timestamp"] == 99
    assert not text.endswith("\n")


def test_subsystem_counters_use_the_same_collection_and_failure_isolation():
    class Broken:
        def counters(self):
            raise RuntimeError("unavailable")

    source = types.SimpleNamespace(counters=lambda: Counters("jobs", "mail", {"run_errors": 3}))
    snap = _Snap(0, 0, [], _Loss())
    bridge = emf.EmfBridge(_Src(snap), counter_sources=(Broken(), source), namespace="Trailhead")
    blob = bridge.blobs(snap, timestamp_ms=99)[-1]
    assert blob["Instance"] == "mail"
    assert blob["jobs_run_errors"] == 3
    assert _metric_names(blob) == {"jobs_run_errors": "None"}


def test_subsystem_counters_obey_delta_mode_and_leave_gauges_absolute():
    values = {"run_errors": 3, "ready": 1}
    source = types.SimpleNamespace(
        counters=lambda: Counters("jobs", "mail", values, gauges=frozenset({"ready"}))
    )
    snap = _Snap(0, 0, [], _Loss())
    bridge = emf.EmfBridge(_Src(snap), counter_sources=(source,))

    first = bridge.blobs(snap, timestamp_ms=1)[-1]
    second = bridge.blobs(snap, timestamp_ms=2)[-1]
    values["run_errors"] = 5
    third = bridge.blobs(snap, timestamp_ms=3)[-1]

    assert first["jobs_run_errors"] == 3
    assert second["jobs_run_errors"] == 0
    assert third["jobs_run_errors"] == 2
    assert first["jobs_ready"] == second["jobs_ready"] == third["jobs_ready"] == 1

    values["run_errors"] = 1
    reset = bridge.blobs(snap, timestamp_ms=4)[-1]
    assert reset["jobs_run_errors"] == 1


def test_subsystem_counters_obey_cumulative_mode():
    source = types.SimpleNamespace(counters=lambda: Counters("jobs", "mail", {"run_errors": 3}))
    snap = _Snap(0, 0, [], _Loss())
    bridge = emf.EmfBridge(_Src(snap), counter_sources=(source,), cumulative=True)
    bridge.blobs(snap, timestamp_ms=1)
    second = bridge.blobs(snap, timestamp_ms=2)[-1]
    assert second["jobs_run_errors"] == 3


def test_subsystem_counter_kernel_preserves_signed_gauges_and_large_integers():
    values = {"temperature": -7, "huge": 1 << 100}
    source = types.SimpleNamespace(
        counters=lambda: Counters("workers", "alpha", values, gauges=frozenset({"temperature"}))
    )
    snap = _Snap(0, 0, [], _Loss())
    bridge = emf.EmfBridge(_Src(snap), counter_sources=(source,))

    first = bridge.blobs(snap, timestamp_ms=1)[-1]
    values["temperature"] = -9
    values["huge"] += 5
    second = bridge.blobs(snap, timestamp_ms=2)[-1]

    assert first["workers_temperature"] == -7
    assert first["workers_huge"] == 1 << 100
    assert second["workers_temperature"] == -9
    assert second["workers_huge"] == 5


def test_render_appends_subsystem_counter_blobs() -> None:
    source = types.SimpleNamespace(counters=lambda: Counters("jobs", "mail", {"run_errors": 3}))
    snap = _Snap(0, 0, [], _Loss())
    text = emf.EmfBridge(_Src(snap), counter_sources=(source,)).render(timestamp_ms=1)
    blobs = [json.loads(line) for line in text.splitlines()]
    assert blobs[-1]["jobs_run_errors"] == 3


def test_native_counter_documents_match_the_independent_python_definition() -> None:
    readings = (
        Counters(
            "jobs",
            "mail",
            types.MappingProxyType({f"metric_{index}": index for index in range(101)}),
            gauges=frozenset({"metric_0"}),
        ),
    )
    dimensions = {"Service": "api", "Instance": "configured"}
    expected = emf._counter_blobs(
        readings,
        timestamp_ms=99,
        namespace="Trailhead",
        dimensions=dimensions,
        cumulative=False,
        deltas={},
        lock=emf.threading.Lock(),
    )
    snap = _Snap(0, 0, [], _Loss())
    state = emf._core.metric_delta_state()
    rendered = emf._core.emf_render(
        snap,
        99,
        None,
        "Trailhead",
        dimensions,
        None,
        False,
        state,
        100,
        readings,
    )
    actual = [json.loads(line) for line in rendered.splitlines()][1:]
    assert actual == expected


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_"):
            _f()
            print(f"ok  {_n}")
    print("emf tests passed")
