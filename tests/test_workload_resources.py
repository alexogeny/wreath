from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import workload_resources
from benchmarks.workload_resources import SCENARIOS, expected, run_case, verify

_SOURCE = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("name", SCENARIOS)
async def test_small_workloads_match_wire_and_sql_oracles(name, monkeypatch):
    monkeypatch.setenv("WREATH_HARDENING", "off")
    elapsed, output, paths = await run_case(name, 2, 2, _SOURCE)
    assert elapsed > 0
    assert output["responses"] == 2
    assert output["sql_operations"] == len(expected(name)[2]) * 2
    assert len(output["sha256"]) == 64
    assert len(paths) == 4


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_op_cannot_pass_workload_oracle(name):
    with pytest.raises(ValueError, match="response count"):
        verify(name, [], [], [], 1)


@pytest.mark.parametrize("field, value", [("status", 201), ("body", b""), ("headers", [])])
def test_wrong_wire_response_is_rejected(field, value):
    body, headers, _ = expected("plaintext")
    response = SimpleNamespace(status=200, headers=headers, body=body)
    setattr(response, field, value)
    with pytest.raises(ValueError, match="wire oracle"):
        verify("plaintext", [response], [], [], 1)


@pytest.mark.parametrize("name", ["point-read", "fan-out-8", "template", "transaction"])
def test_correct_body_without_database_operations_is_rejected(name):
    body, headers, _ = expected(name)
    response = SimpleNamespace(status=200, headers=headers, body=body)
    with pytest.raises(ValueError, match="SQL operations"):
        verify(name, [response], [], [], 1)


def test_extra_database_work_is_rejected():
    body, headers, _ = expected("plaintext")
    response = SimpleNamespace(status=200, headers=headers, body=body)
    with pytest.raises(ValueError, match="SQL operations"):
        verify("plaintext", [response], ["SELECT 1"], [[b"S"]], 1)


def test_wrong_flight_boundary_is_rejected():
    body, headers, sql = expected("point-read")
    response = SimpleNamespace(status=200, headers=headers, body=body)
    with pytest.raises(ValueError, match="Sync-delimited"):
        verify("point-read", [response], sql, [[b"S", b"S"]], 1)


def test_transaction_operation_order_is_verified_not_only_count():
    body, headers, sql = expected("transaction")
    response = SimpleNamespace(status=200, headers=headers, body=body)
    sql[1], sql[3] = sql[3], sql[1]
    with pytest.raises(ValueError, match="SQL operations"):
        verify("transaction", [response], sql, [[b"S"] for _ in sql], 1)


async def test_wrong_source_root_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="source root"):
        await run_case("plaintext", 1, 2, tmp_path)


async def test_cpu_phase_excludes_warmup_and_verification(monkeypatch):
    from wreath.testing import TestClient

    monkeypatch.setenv("WREATH_HARDENING", "off")
    original = TestClient.request
    completed = 0
    sampled = []

    async def request(*args, **kwargs):
        nonlocal completed
        response = await original(*args, **kwargs)
        completed += 1
        return response

    def clock():
        sampled.append(completed)
        return len(sampled) * 100

    def checked(*args):
        assert sampled == [2, 5]
        return verify(*args)

    monkeypatch.setattr(TestClient, "request", request)
    monkeypatch.setattr(workload_resources, "process_time_ns", clock)
    monkeypatch.setattr(workload_resources, "verify", checked)
    elapsed, output, _ = await run_case("plaintext", 3, 2, _SOURCE)
    assert elapsed == 100
    assert output["responses"] == 3
    assert sampled == [2, 5]
