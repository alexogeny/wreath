from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "arm",
    ["route", "cors", "binding", "auth", "cedar", "postgres", "complete-aa"],
)
def test_e2e_stack_imports_one_valid_wreath_application(arm: str) -> None:
    code = (
        "import json; from benchmarks import e2e_stack as s; "
        "print(json.dumps({'framework': s.FRAMEWORK, 'arm': s.ARM, "
        "'effective': s.EFFECTIVE_ARM, 'expected': s.EXPECTED, "
        "'app': callable(s.app)}))"
    )
    env = dict(os.environ)
    env["WREATH_E2E_FRAMEWORK"] = "wreath"
    env["WREATH_E2E_ARM"] = arm
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout)
    assert payload["framework"] == "wreath"
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
    for framework in ("wreath", "fastapi"):
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
    fastapi = round(artifact["arms"]["fastapi"]["complete"]["median"])
    ratio = fastapi / wreath
    assert f"{wreath:,}" in readme
    assert f"{fastapi:,}" in readme
    assert f"{ratio:.2f}× fewer instructions" in readme


def test_retained_holistic_account_drives_the_readme_hero() -> None:
    artifact = json.loads(
        (ROOT / "docs/perf/data/e2e-holistic-stack-instructions.json").read_text()
    )
    assert artifact["metric"] == "retired userspace instructions per successful request"
    assert artifact["measurement"]["trials"] == 5
    assert artifact["measurement"]["requests_high"] == 30
    assert artifact["measurement"]["requests_low"] == 15

    assert artifact["transport"] == "TLS 1.3 over HTTP/1.1 for every arm"
    for framework in ("wreath", "wreath-optimal", "fastapi"):
        rows = artifact["arms"][framework]
        assert tuple(rows) == ("holistic", "holistic-aa")
        assert all(len(row["samples"]) == 5 for row in rows.values())
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
