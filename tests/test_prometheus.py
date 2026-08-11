"""Prometheus exposition bridge — renderer format + bridge behavior.

The renderer is duck-typed over a projector snapshot, so these fall back to loading
``src/wreath/_prometheus.py`` by path and run under a bare ``/usr/bin/python3``
(no native build). The handler test needs the built package (``Response``) and is
skipped when it is absent.

The real ``wreath._prometheus`` is preferred whenever it imports, following
``test_statsd.py``. A by-path load defeats `wreath mutant`: it execs pristine source
into a *second* module object, so a mutation applied to ``wreath._prometheus`` in the
forked child's memory never reaches the code under test and every mutant here reports
`survived` while these tests pass.
"""
from __future__ import annotations

import importlib
import importlib.util
import pathlib
import re

_PROM_PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "wreath" / "_prometheus.py"
try:
    prom = importlib.import_module("wreath._prometheus")
except ImportError:
    _spec = importlib.util.spec_from_file_location("wreath._prometheus", _PROM_PATH)
    prom = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(prom)  # top-level imports are stdlib only


# --- duck-typed snapshot fixtures ------------------------------------------

class _Loss:
    _FIELDS = (
        "orphan_phase", "orphan_correlation", "pending_evicted",
        "decode_error", "export_error", "recent_evicted",
    )

    def __init__(self, **kw: int) -> None:
        for f in self._FIELDS:
            setattr(self, f, int(kw.get(f, 0)))


class _Route:
    def __init__(self, route_id, count, errors, dsum, dmax, buckets):
        self.route_id = route_id
        self.count = count
        self.errors = errors
        self.duration_us_sum = dsum
        self.duration_us_max = dmax
        self.buckets = buckets


class _Snap:
    def __init__(self, assembled, pending, routes, loss):
        self.assembled = assembled
        self.pending = pending
        self.routes = routes
        self.loss = loss


class _Reason:  # mimics a LossReason enum member (has .name)
    def __init__(self, name: str) -> None:
        self.name = name


def _bkts(**idx: int) -> list[int]:
    b = [0] * 64
    for i, v in idx.items():
        b[int(i)] = v
    return b


_SAMPLE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? (\+Inf|-Inf|NaN|-?[0-9.eE+-]+)$")


def _parse(text: str):
    """Validate structure; return {family: [(labels_str, value)]}. Asserts every
    sample has a preceding ``# TYPE`` for its family and every line is well-formed."""
    types: dict[str, str] = {}
    samples: dict[str, list[tuple[str, str]]] = {}
    assert text.endswith("\n")
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# TYPE "):
            _, _, rest = line.partition("# TYPE ")
            name, kind = rest.split(" ")
            types[name] = kind
            continue
        if line.startswith("#"):
            continue
        assert _SAMPLE.match(line), f"bad sample line: {line!r}"
        name = line.split("{", 1)[0].split(" ", 1)[0]
        family = name
        for suf in ("_bucket", "_sum", "_count"):
            if family.endswith(suf):
                family = family[: -len(suf)]
                break
        assert family in types, f"sample {name} has no declared TYPE (family {family})"
        labels = line[len(name):].rsplit(" ", 1)[0].strip() if "{" in line else ""
        value = line.rsplit(" ", 1)[1]
        samples.setdefault(name, []).append((labels, value))
    return types, samples


# --- tests ------------------------------------------------------------------

def test_counter_gauge_and_globals():
    snap = _Snap(
        assembled=42, pending=3,
        routes=[_Route(7, count=10, errors=2, dsum=5000, dmax=900, buckets=_bkts())],
        loss=_Loss(decode_error=1, export_error=4),
    )
    text = prom.render_exposition(snap)
    types, samples = _parse(text)
    assert types["wreath_http_requests_total"] == "counter"
    assert types["wreath_flight_pending"] == "gauge"
    assert types["wreath_flight_traces_assembled_total"] == "counter"
    assert ('{route_id="7"}', "10") in samples["wreath_http_requests_total"]
    assert ('{route_id="7"}', "2") in samples["wreath_http_request_errors_total"]
    assert ("", "42") in samples["wreath_flight_traces_assembled_total"]
    assert ("", "3") in samples["wreath_flight_pending"]
    # projector loss: one sample per field, with the failing reasons set.
    loss = dict(samples["wreath_flight_projector_loss_total"])
    assert loss['{reason="decode_error"}'] == "1"
    assert loss['{reason="export_error"}'] == "4"
    assert loss['{reason="orphan_phase"}'] == "0"


def test_histogram_cumulative_and_inf():
    # buckets: 3 obs in [2,4)us (bucket 1), 2 obs in [32,64)us (bucket 5).
    snap = _Snap(
        assembled=5, pending=0,
        routes=[_Route(1, count=5, errors=0, dsum=1234, dmax=63,
                       buckets=_bkts(**{"1": 3, "5": 2}))],
        loss=_Loss(),
    )
    text = prom.render_exposition(snap)
    types, samples = _parse(text)
    assert types["wreath_http_request_duration_seconds"] == "histogram"
    buckets = samples["wreath_http_request_duration_seconds_bucket"]
    # le boundaries emitted only where a bucket had observations, cumulative + +Inf.
    le_to_count = {lbl: int(v) for lbl, v in buckets}
    # bucket 1 upper edge = 2**2 us = 4e-06 s, cumulative 3
    assert le_to_count['{route_id="1",le="4e-06"}'] == 3
    # bucket 5 upper edge = 2**6 us = 6.4e-05 s, cumulative 5
    assert le_to_count['{route_id="1",le="6.4e-05"}'] == 5
    assert le_to_count['{route_id="1",le="+Inf"}'] == 5
    # monotonic non-decreasing cumulative counts
    counts = [int(v) for _, v in buckets]
    assert counts == sorted(counts)
    assert dict(samples["wreath_http_request_duration_seconds_count"])['{route_id="1"}'] == "5"
    assert dict(samples["wreath_http_request_duration_seconds_sum"])['{route_id="1"}'] == "0.001234"
    assert dict(samples["wreath_http_request_duration_max_seconds"])['{route_id="1"}'] == "6.3e-05"


def test_route_labels_and_escaping():
    def labeller(route_id):
        return {"method": "GET", "path": 'a"b\\c\nd'}

    snap = _Snap(1, 0, [_Route(9, 1, 0, 0, 0, _bkts())], _Loss())
    text = prom.render_exposition(snap, route_labels=labeller)
    # quote, backslash, and newline in a label value are escaped.
    assert 'method="GET",path="a\\"b\\\\c\\nd"' in text
    _parse(text)  # still structurally valid


def test_native_default_route_rendering_matches_labelled_definition():
    routes = [
        _Route('a"b\\c\nd', 5, 1, 1234, 63, _bkts(**{"1": 3, "5": 2})),
        _Route(9, True, False, 0, 0, _bkts()),
        _Route(10, 1 << 100, 0, 9_876_543, 1_234, [1 << 80] + [0] * 63),
    ]
    snap = _Snap(7, 0, routes, _Loss())
    native = prom.render_exposition(snap)
    defined = prom.render_exposition(
        snap,
        route_labels=lambda route_id: {"route_id": str(route_id)},
    )
    assert native == defined


def test_name_sanitization():
    snap = _Snap(0, 0, [], _Loss())
    text = prom.render_exposition(snap, namespace="my app-1")
    # invalid namespace chars → underscores; families are valid metric names.
    assert "my_app_1_flight_pending" in text
    _parse(text)


def test_empty_snapshot_still_valid():
    text = prom.render_exposition(_Snap(0, 0, [], _Loss()))
    types, samples = _parse(text)
    # families are declared even with no routes; globals present.
    assert "wreath_http_requests_total" in types
    assert samples["wreath_flight_traces_assembled_total"] == [("", "0")]


def test_recorder_loss_reason_labels():
    snap = _Snap(0, 0, [], _Loss())
    rec = {_Reason("RING_FULL"): 4, _Reason("EXPORT_QUEUE_FULL"): 0, "propagation_invalid": 2}
    text = prom.render_exposition(snap, recorder_loss=rec)
    _, samples = _parse(text)
    got = dict(samples["wreath_flight_recorder_loss_total"])
    assert got['{reason="ring_full"}'] == "4"
    assert got['{reason="export_queue_full"}'] == "0"
    assert got['{reason="propagation_invalid"}'] == "2"


def test_openmetrics_variant():
    snap = _Snap(
        assembled=9, pending=1,
        routes=[_Route(1, count=5, errors=1, dsum=1234, dmax=63, buckets=_bkts(**{"1": 5}))],
        loss=_Loss(),
    )
    text = prom.render_exposition(snap, openmetrics=True)
    # OpenMetrics terminates with `# EOF`.
    assert text.endswith("# EOF\n")
    # Counter families drop the `_total` suffix on the TYPE line; samples keep it.
    assert "# TYPE wreath_http_requests counter" in text
    assert "# TYPE wreath_http_requests_total counter" not in text
    assert re.search(r'^wreath_http_requests_total\{route_id="1"\} 5$', text, re.M)
    # Histogram/gauge families are unchanged (no _total suffix to strip).
    assert "# TYPE wreath_http_request_duration_seconds histogram" in text
    assert "wreath_http_request_duration_seconds_count" in text
    # The dedicated content type is exported.
    assert prom.OPENMETRICS_CONTENT_TYPE == (
        "application/openmetrics-text; version=1.0.0; charset=utf-8"
    )


def test_bridge_render_reads_source():
    class _Source:
        def __init__(self):
            self._snap = _Snap(2, 1, [_Route(3, 4, 0, 0, 0, _bkts())], _Loss())
        def snapshot(self):
            return self._snap
        def recorder_loss(self):
            return {_Reason("RING_FULL"): 1}

    bridge = prom.PrometheusBridge(_Source(), namespace="svc")
    text = bridge.render()
    assert "svc_http_requests_total" in text
    assert 'svc_flight_recorder_loss_total{reason="ring_full"} 1' in text


def test_bridge_rejects_bad_source():
    import pytest

    with pytest.raises(TypeError):
        prom.PrometheusBridge(object())


def test_handler_content_type():
    import pytest

    pytest.importorskip("wreath.response")
    from wreath._prometheus import PrometheusBridge

    class _Source:
        def snapshot(self):
            return _Snap(0, 0, [], _Loss())

    handler = PrometheusBridge(_Source()).handler()
    import asyncio

    response = asyncio.run(handler(None))
    ctype = dict(response.headers).get(b"content-type")
    assert ctype == b"text/plain; version=0.0.4; charset=utf-8"
    assert response.status == 200
    assert b"wreath_flight_pending" in response.body


if __name__ == "__main__":  # standalone: run the renderer/bridge tests without pytest
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and _name not in (
            "test_bridge_rejects_bad_source", "test_handler_content_type",
        ):
            _fn()
            print(f"ok  {_name}")
    print("renderer tests passed")
