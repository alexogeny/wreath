from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.bench_holistic_stack_instructions import (
    _derive_metrics,
    _parse_counters,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "arm",
    ["route", "cors", "binding", "auth", "cedar", "postgres", "complete-aa"],
)
@pytest.mark.parametrize("framework", ["wreath", "fastapi", "sanic", "blacksheep"])
def test_e2e_stack_imports_one_valid_application(framework: str, arm: str) -> None:
    code = (
        "import json; from benchmarks import e2e_stack as s; "
        "print(json.dumps({'framework': s.FRAMEWORK, 'arm': s.ARM, "
        "'effective': s.EFFECTIVE_ARM, 'expected': s.EXPECTED, "
        "'app': callable(s.app)}))"
    )
    env = dict(os.environ)
    env["WREATH_E2E_FRAMEWORK"] = framework
    env["WREATH_E2E_ARM"] = arm
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout)
    assert payload["framework"] == framework
    assert payload["arm"] == arm
    assert payload["effective"] == ("complete" if arm == "complete-aa" else arm)
    assert payload["expected"]["db"] == 42
    assert payload["app"] is True


def test_retained_instruction_account_has_repeated_slopes_and_aa_controls() -> None:
    artifact = json.loads((ROOT / "docs/perf/data/e2e-stack-instructions.json").read_text())
    assert artifact["metric"] == "retired userspace instructions per successful request"
    assert artifact["measurement"]["trials"] == 5
    assert artifact["measurement"]["requests_high"] == 4_000
    assert artifact["measurement"]["requests_low"] == 2_000
    assert tuple(artifact["arms"]) == ("wreath", "fastapi", "sanic", "blacksheep")
    for framework in ("wreath", "fastapi", "sanic", "blacksheep"):
        rows = artifact["arms"][framework]
        assert tuple(rows) == (
            "route",
            "cors",
            "binding",
            "auth",
            "cedar",
            "postgres",
            "complete",
            "complete-aa",
        )
        assert all(len(row["samples"]) == 5 for row in rows.values())
        assert rows["complete-aa"]["absolute_delta_from_complete"] >= 0


def test_readme_histogram_matches_the_retained_complete_medians() -> None:
    artifact = json.loads((ROOT / "docs/perf/data/e2e-stack-instructions.json").read_text())
    readme = (ROOT / "README.md").read_text()
    wreath = round(artifact["arms"]["wreath"]["complete"]["median"])
    assert f"{wreath:,}" in readme
    for framework in ("fastapi", "sanic", "blacksheep"):
        complete = round(artifact["arms"][framework]["complete"]["median"])
        ratio = complete / wreath
        assert f"{complete:,}" in readme
        assert f"{ratio:.2f}× fewer" in readme


def test_retained_holistic_account_drives_the_readme_hero() -> None:
    artifact = json.loads(
        (ROOT / "docs/perf/data/e2e-holistic-stack-instructions.json").read_text()
    )
    assert artifact["metric"] == "retired userspace instructions per successful request"
    assert artifact["schema"] == "wreath/e2e-holistic-stack-counters/4"
    assert artifact["measurement"]["trials"] == 5
    assert artifact["measurement"]["requests_high"] == 30
    assert artifact["measurement"]["requests_low"] == 15

    assert artifact["transport"] == "TLS 1.3 over HTTP/1.1 for every arm"
    assert tuple(artifact["arms"]) == (
        "wreath",
        "wreath-optimal",
        "fastapi",
        "sanic",
        "blacksheep",
    )
    for framework in ("wreath", "wreath-optimal", "fastapi", "sanic", "blacksheep"):
        rows = artifact["arms"][framework]
        assert tuple(rows) == ("holistic", "holistic-aa")
        assert all(len(row["samples"]) == 5 for row in rows.values())
        assert all(
            tuple(row["counters"])
            == (
                "instructions",
                "l1d_hits",
                "l1d_misses",
                "l1i_hits",
                "l1i_misses",
                "l2_demand_hits",
                "l2_demand_misses",
                "l2_prefetch_hits",
                "l2_prefetch_misses",
                "l2_all_hits",
                "l2_all_misses",
            )
            for row in rows.values()
        )
        assert all(
            len(counter["samples"]) == 5
            for row in rows.values()
            for counter in row["counters"].values()
        )
        assert rows["holistic-aa"]["absolute_delta_from_holistic"] >= 0

    readme = (ROOT / "README.md").read_text()
    wreath = round(artifact["arms"]["wreath"]["holistic"]["median"])
    optimal = round(artifact["arms"]["wreath-optimal"]["holistic"]["median"])
    fastapi = round(artifact["arms"]["fastapi"]["holistic"]["median"])
    ratio = fastapi / wreath
    optimal_ratio = fastapi / optimal
    assert f"{wreath:,}" in readme
    assert f"{optimal:,}" in readme
    assert f"{fastapi:,}" in readme
    assert f"{ratio:.2f}× fewer instructions" in readme
    assert f"{optimal_ratio:.2f}× fewer instructions" in readme
    for framework in ("wreath", "wreath-optimal", "fastapi", "sanic", "blacksheep"):
        holistic = artifact["arms"][framework]["holistic"]
        counters = holistic["counters"]
        assert f"{holistic['range'][0] / 1_000_000:.3f}M" in readme
        assert f"{holistic['range'][1] / 1_000_000:.3f}M" in readme
        for name in (
            "l1d_hits",
            "l1d_misses",
            "l1i_hits",
            "l1i_misses",
            "l2_demand_hits",
            "l2_demand_misses",
            "l2_prefetch_hits",
            "l2_prefetch_misses",
            "l2_all_misses",
        ):
            assert f"{round(counters[name]['median']):,}" in readme


def test_holistic_counter_parser_names_every_required_event() -> None:
    events = {
        "instructions": "instructions:u",
        "l1d_misses": "l1-dcache-load-misses:u",
    }
    stderr = (
        "12345;;instructions:u;99;100.00;;\n"
        "678;;l1-dcache-load-misses:u;99;100.00;;\n"
    )
    assert _parse_counters(stderr, events) == {
        "instructions": 12_345,
        "l1d_misses": 678,
    }


def test_holistic_wreath_rebuilds_the_chart_projection_per_request() -> None:
    source = (ROOT / "benchmarks/holistic_e2e.py").read_text()
    assert "project_chart_text(" in source
    assert "_SERIES_DATA" not in source
    assert "ChartData" not in source


def test_holistic_derived_cache_hits_use_accesses_less_misses() -> None:
    assert _derive_metrics(
        {
            "instructions": 1_000.0,
            "l1d_accesses": 400.0,
            "l1d_misses": 20.0,
            "l1i_accesses": 300.0,
            "l1i_misses": 10.0,
            "l2_demand_hits": 100.0,
            "l2_prefetch_hits": 30.0,
            "l2_demand_misses": 8.0,
            "l2_prefetch_hits_l3": 4.0,
            "l2_prefetch_misses_l3": 2.0,
        }
    ) == {
        "instructions": 1_000.0,
        "l1d_hits": 380.0,
        "l1d_misses": 20.0,
        "l1i_hits": 290.0,
        "l1i_misses": 10.0,
        "l2_demand_hits": 100.0,
        "l2_demand_misses": 8.0,
        "l2_prefetch_hits": 30.0,
        "l2_prefetch_misses": 6.0,
        "l2_all_hits": 130.0,
        "l2_all_misses": 14.0,
    }
