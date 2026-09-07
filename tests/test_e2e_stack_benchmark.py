from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from benchmarks.bench_holistic_stack_instructions import (
    _derive_metrics,
    _parse_counters,
    _parse_smaps_rollup,
    _server_command,
    _summary,
)

ROOT = Path(__file__).resolve().parents[1]


def test_holistic_counter_helpers_do_not_import_benchmark_frameworks() -> None:
    code = (
        "import sys; import benchmarks.bench_holistic_stack_instructions; "
        "assert 'benchmarks.holistic_fastapi' not in sys.modules; "
        "assert 'aiohttp' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_holistic_app_does_not_construct_the_unrelated_benchmark_app() -> None:
    code = (
        "import sys; import benchmarks.holistic_e2e; "
        "assert 'benchmarks.apps' not in sys.modules; "
        "assert 'benchmarks.scenarios' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("framework", ["wreath", "fastapi", "sanic", "blacksheep"])
def test_e2e_stack_imports_one_valid_application(framework: str) -> None:
    arms = ("route", "cors", "binding", "auth", "cedar", "postgres", "complete-aa")
    code = "\n".join(
        (
            "import importlib, json, os, sys",
            f"arms = {arms!r}",
            "rows = []",
            "for arm in arms:",
            "    os.environ['WREATH_E2E_ARM'] = arm",
            "    sys.modules.pop('benchmarks.e2e_stack', None)",
            "    stack = importlib.import_module('benchmarks.e2e_stack')",
            "    rows.append({",
            "        'framework': stack.FRAMEWORK, 'arm': stack.ARM,",
            "        'effective': stack.EFFECTIVE_ARM, 'expected': stack.EXPECTED,",
            "        'app': callable(stack.app),",
            "    })",
            "print(json.dumps(rows))",
        )
    )
    env = dict(os.environ)
    env["WREATH_E2E_FRAMEWORK"] = framework
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payloads = json.loads(result.stdout)
    assert len(payloads) == len(arms)
    for arm, payload in zip(arms, payloads, strict=True):
        assert payload["framework"] == framework
        assert payload["arm"] == arm
        assert payload["effective"] == ("complete" if arm == "complete-aa" else arm)
        assert payload["expected"]["db"] == 42
        assert payload["app"] is True


def test_retained_instruction_account_has_repeated_slopes_and_aa_controls() -> None:
    artifact = json.loads((ROOT / "benchmarks/baselines/e2e-stack-instructions.json").read_text())
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
    limitation = artifact["limitations"]
    assert "hand-written msgspec success-path adapter" in limitation
    assert "rather than equivalent framework feature costs" in limitation


def test_benchmark_guide_marks_non_equivalent_sanic_and_blacksheep_steps() -> None:
    artifact = json.loads((ROOT / "benchmarks/baselines/e2e-stack-instructions.json").read_text())
    guide = " ".join((ROOT / "benchmarks/README.md").read_text().split())
    assert "hand-written msgspec success-path adapter" in guide
    assert "not either framework's full validation/authentication surface" in guide
    assert "every later cumulative cell inherits the limitation" in guide
    assert "hand-written msgspec success-path adapter" in artifact["limitations"]


def test_readme_animation_dependency_stays_out_of_the_runtime() -> None:
    package = json.loads((ROOT / "tools/readme_charts/package.json").read_text())
    assert package["private"] is True
    assert package["devDependencies"] == {"animejs": "4.5.0"}
    assert "animejs" not in (ROOT / "pyproject.toml").read_text()
    assert 'name = "animejs"' not in (ROOT / "uv.lock").read_text()


def test_retained_holistic_account_drives_the_readme_hero() -> None:
    artifact = json.loads(
        (ROOT / "benchmarks/baselines/e2e-holistic-stack-instructions.json").read_text()
    )
    assert artifact["metric"] == "retired userspace instructions per successful request"
    assert artifact["schema"] == "wreath/e2e-holistic-stack-counters/5"
    assert artifact["measurement"]["trials"] == 5
    assert artifact["measurement"]["requests_high"] == 30
    assert artifact["measurement"]["requests_low"] == 15
    assert artifact["measurement"]["counter_profile"] == "instructions"
    assert artifact["measurement"]["memory"] == {
        "enabled": True,
        "trials": 5,
        "requests": 30,
        "sample_interval_ms": 2.0,
        "process_scope": "server root and every descendant",
        "collector": "/proc/<pid>/smaps_rollup",
        "counter_pass_separate": True,
    }
    assert "shorter-lived peak may be missed" in artifact["memory_limitations"]
    assert "procfs reads cannot perturb" in artifact["fairness"]

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
        assert all(tuple(row["counters"]) == ("instructions",) for row in rows.values())
        assert all(
            len(counter["samples"]) == 5
            for row in rows.values()
            for counter in row["counters"].values()
        )
        assert rows["holistic-aa"]["absolute_delta_from_holistic"] >= 0
        memory = artifact["memory"][framework]
        assert tuple(memory) == (
            "ready",
            "verified",
            "warmed",
            "retained",
            "observed_peak",
        )
        assert all(tuple(stage) == ("pss_bytes", "rss_bytes") for stage in memory.values())
        assert all(
            len(metric["samples"]) == 5
            and metric["median"] > 0
            and metric["mad"] >= 0
            for stage in memory.values()
            for metric in stage.values()
        )

    source = (ROOT / "benchmarks/baselines/e2e-holistic-stack-instructions.json").read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    readme = (ROOT / "README.md").read_text()
    instruction_chart = (ROOT / "docs/assets/readme/holistic-instructions.svg").read_text()
    memory_chart = (ROOT / "docs/assets/readme/holistic-memory.svg").read_text()
    assert 'src="docs/assets/readme/holistic-instructions.svg"' in readme
    assert 'src="docs/assets/readme/holistic-memory.svg"' in readme
    for chart in (instruction_chart, memory_chart):
        assert f'data-source-sha256="{source_hash}"' in chart
        assert 'data-generator="animejs-4.5.0"' in chart
        assert "<animate " in chart
    for framework in ("wreath", "wreath-optimal", "fastapi", "sanic", "blacksheep"):
        holistic = artifact["arms"][framework]["holistic"]
        assert (
            f'data-stack="{framework}" '
            f'data-median-instructions="{holistic["median"]:.3f}"'
        ) in instruction_chart
        pss = ",".join(
            f'{artifact["memory"][framework][stage]["pss_bytes"]["median"] / (1024 * 1024):.3f}'
            for stage in ("ready", "verified", "warmed", "retained")
        )
        peak_rss = (
            Decimal(
                artifact["memory"][framework]["observed_peak"]["rss_bytes"]["median"]
            )
            / Decimal(1024 * 1024)
        ).quantize(
            Decimal("0.001"),
            rounding=ROUND_HALF_UP,
        )
        assert f'data-stack="{framework}" data-pss-mib="{pss}"' in memory_chart
        assert f'data-peak-rss-mib="{peak_rss}"' in memory_chart


def test_holistic_counter_parser_names_every_required_event() -> None:
    events = {
        "instructions": "instructions:u",
        "l1d_misses": "l1-dcache-load-misses:u",
    }
    stderr = "12345;;instructions:u;99;100.00;;\n678;;l1-dcache-load-misses:u;99;100.00;;\n"
    assert _parse_counters(stderr, events) == {
        "instructions": 12_345,
        "l1d_misses": 678,
    }


def test_holistic_counter_parser_sums_supported_hybrid_pmus() -> None:
    stderr = (
        "<not counted>;;cpu_atom/instructions/u;0;0.00;;\n"
        "12345;;cpu_core/instructions/u;99;100.00;;\n"
    )
    assert _parse_counters(stderr, {"instructions": "instructions:u"}) == {
        "instructions": 12_345
    }


def test_holistic_memory_parser_reads_pss_and_rss_as_bytes() -> None:
    assert _parse_smaps_rollup("Rss: 123 kB\nPss: 45 kB\n") == {
        "pss_bytes": 45 * 1024,
        "rss_bytes": 123 * 1024,
    }


def test_holistic_summary_retains_median_absolute_deviation() -> None:
    assert _summary([10.0, 12.0, 14.0, 100.0]) == {
        "median": 13.0,
        "mad": 2.0,
        "range": [10.0, 100.0],
        "samples": [10.0, 12.0, 14.0, 100.0],
    }


def test_holistic_fastapi_server_command_has_one_port_option(tmp_path: Path) -> None:
    command = _server_command("fastapi", 8123, 8, tmp_path / "cert", tmp_path / "key")
    assert command.count("--port") == 1
    assert command[command.index("--port") + 1] == "8123"


def test_holistic_wreath_reuses_compact_chart_data_without_caching_the_projection() -> None:
    source = (ROOT / "benchmarks/holistic_e2e.py").read_text()
    assert "_SERIES_CHART = ChartData.from_rows(" in source
    assert "_SERIES_SPARSE" not in source
    assert "_SERIES_CHART.project_chart_text_joined(" in source
    assert '"".join(paths)' not in source
    assert "cache=False" in source


def test_holistic_optimal_compression_renders_only_the_dynamic_prefix() -> None:
    source = (ROOT / "benchmarks/holistic_e2e.py").read_text()
    assert '_COMPRESSION._dcz_fragment_render(request, "html", _PAGE_PREFIX, context)' in source


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
