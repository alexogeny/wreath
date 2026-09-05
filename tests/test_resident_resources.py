import runpy
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def harness():
    root = Path(__file__).resolve().parents[1]
    return SimpleNamespace(**runpy.run_path(str(root / "benchmarks/resident_resources.py")))


def test_resident_harness_refuses_active_tracing(harness, monkeypatch):
    monkeypatch.setattr(harness.tracemalloc, "is_tracing", lambda: True)
    with pytest.raises(RuntimeError, match="tracemalloc must be disabled"):
        harness.require_untraced()


def test_resident_reader_requires_both_kernel_fields(harness, tmp_path):
    path = tmp_path / "smaps"
    path.write_text("Rss: 12 kB\nPss: 9 kB\nShared_Clean: 4 kB\n")
    assert harness.resident_bytes(path) == {"rss_bytes": 12288, "pss_bytes": 9216}
    path.write_text("Rss: 12 kB\n")
    with pytest.raises(RuntimeError, match="Rss and Pss"):
        harness.resident_bytes(path)


@pytest.mark.parametrize(
    "scenario,size",
    [
        ("record-batch", 4),
        ("response-validators", 3),
        ("route-masks", 4),
        ("application-image", 3),
        ("response-capture", 128),
        ("object-range", 4096),
        ("tiny-objects", 3),
    ],
)
async def test_small_resident_scenarios_validate_outputs_and_provenance(harness, scenario, size):
    metrics, output = await harness.measure(scenario, size, SOURCE_ROOT, chunk_bytes=16)
    assert output["scenario"] == scenario
    assert output["size"] == size
    assert metrics["tracing_enabled"] is False
    assert metrics["rss_bytes"] > 0
    assert metrics["pss_bytes"] > 0
    assert metrics["growth_rss_bytes"] == metrics["rss_bytes"] - metrics["before_rss_bytes"]
    assert metrics["growth_pss_bytes"] == metrics["pss_bytes"] - metrics["before_pss_bytes"]
    assert metrics["artifacts"]
    for artifact in metrics["artifacts"]:
        assert Path(artifact["path"]).is_relative_to(SOURCE_ROOT)
        assert len(artifact["sha256"]) == 64


@pytest.mark.parametrize(
    "scenario,size",
    [
        ("record-batch", 2),
        ("response-validators", 2),
        ("route-masks", 2),
        ("application-image", 2),
        ("response-capture", 32),
        ("object-range", 4096),
        ("tiny-objects", 2),
    ],
)
async def test_resident_oracles_reject_no_operation(harness, scenario, size):
    case = await harness.prepare(scenario, size, 16)
    with pytest.raises(RuntimeError):
        await case.verify(None)


async def test_resident_harness_refuses_another_loaded_source_tree(harness, tmp_path):
    with pytest.raises(RuntimeError, match="outside requested source"):
        await harness.measure("tiny-objects", 2, tmp_path)


async def test_resident_boundary_keeps_result_until_after_second_sample(harness, monkeypatch):
    events = []
    references = []

    class Owned:
        pass

    async def operation():
        events.append("operation")
        result = Owned()
        references.append(weakref.ref(result))
        return result

    async def verify(result):
        events.append("verify")
        assert references[0]() is result
        assert events == ["sample", "operation", "sample", "verify"]
        return {"verified": True}

    async def prepare(*args):
        return harness.Case(operation, verify, "owned test boundary", ())

    def resident_bytes():
        events.append("sample")
        if references:
            assert references[0]() is not None
        return {"rss_bytes": 100, "pss_bytes": 50}

    monkeypatch.setitem(harness.measure.__globals__, "prepare", prepare)
    monkeypatch.setitem(harness.measure.__globals__, "resident_bytes", resident_bytes)
    metrics, output = await harness.measure("tiny-objects", 1, SOURCE_ROOT)
    assert output["verified"]
    assert metrics["growth_rss_bytes"] == metrics["growth_pss_bytes"] == 0
    assert references[0]() is None
