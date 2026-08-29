from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _toml(path: str) -> dict:
    with (ROOT / path).open("rb") as stream:
        return tomllib.load(stream)


def _action_uses(value: object) -> set[str]:
    if isinstance(value, dict):
        found = {
            action for key, action in value.items() if key == "uses" and isinstance(action, str)
        }
        return found | set().union(*map(_action_uses, value.values()))
    if isinstance(value, list):
        return set().union(*map(_action_uses, value))
    return set()


def test_release_distributions_share_version_and_explicit_extras() -> None:
    base = _toml("pyproject.toml")
    linux = _toml("packages/wreath-linux/pyproject.toml")
    http3 = _toml("packages/wreath-http3/pyproject.toml")

    version = base["project"]["version"]
    assert linux["project"]["version"] == version
    assert http3["project"]["version"] == version
    assert base["project"]["dependencies"] == []
    assert linux["project"]["dependencies"] == []
    assert http3["project"]["dependencies"] == []
    assert base["project"]["requires-python"] == "==3.14.*"
    assert linux["project"]["requires-python"] == "==3.14.*"
    assert http3["project"]["requires-python"] == "==3.14.*"
    expected_base_install = (
        f"pip install --no-index --find-links {{package}}/wheel-deps wreath=={version}"
    )
    assert linux["tool"]["cibuildwheel"]["before-test"] == expected_base_install
    assert http3["tool"]["cibuildwheel"]["before-test"] == expected_base_install

    extras = base["project"]["optional-dependencies"]
    assert extras == {
        "linux": [f"wreath-linux=={version}; sys_platform == 'linux'"],
        "h3": [f"wreath-http3=={version}; sys_platform == 'linux'"],
        "http3": [f"wreath-http3=={version}; sys_platform == 'linux'"],
    }


def test_base_wheel_smoke_is_portable_and_companions_are_linux_only() -> None:
    base = _toml("pyproject.toml")["tool"]["cibuildwheel"]
    linux = _toml("packages/wreath-linux/pyproject.toml")
    http3 = _toml("packages/wreath-http3/pyproject.toml")

    assert base["test-command"].endswith("wheel_smoke.py --base")
    assert "shutil.rmtree" in base["before-build"]
    assert "rm -rf" not in base["before-build"]
    assert "linux" in linux["tool"]["cibuildwheel"]
    assert "linux" in http3["tool"]["cibuildwheel"]
    assert linux["tool"]["cibuildwheel"]["test-command"].endswith("wheel_smoke.py --reactor")
    assert http3["tool"]["cibuildwheel"]["test-command"].endswith("wheel_smoke.py --http3")


def test_release_is_regular_cpython_314_and_wheel_only() -> None:
    distributions = (
        ROOT,
        ROOT / "packages/wreath-linux",
        ROOT / "packages/wreath-http3",
    )
    for distribution in distributions:
        project = _toml(str(distribution.relative_to(ROOT) / "pyproject.toml"))
        assert project["tool"]["cibuildwheel"]["build"] == "cp314-*"
        setup = (distribution / "setup.py").read_text()
        assert 'sysconfig.get_config_var("Py_GIL_DISABLED")' in setup
        assert "Use a regular CPython 3.14 interpreter." in setup

    workflow_text = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "python -m build --sdist" not in workflow_text
    assert "Build sdist" not in workflow_text


def test_base_build_cleans_stale_capability_outputs() -> None:
    setup = (ROOT / "setup.py").read_text()
    assert "class _CleanBuild(build):" in setup
    assert 'cmdclass={"build": _CleanBuild' in setup


def test_ci_builds_linux_capabilities_before_tests() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["checks"]["steps"]
    commands = [step.get("run", "") for step in steps]
    build_index = commands.index("WREATH_BUILD_LINUX=1 uv run python setup.py build_ext --inplace")
    test_index = next(index for index, command in enumerate(commands) if "wreath test" in command)
    assert build_index < test_index


def test_windows_native_build_enables_c11_atomics_and_owns_pi() -> None:
    setup = (ROOT / "setup.py").read_text()
    geospatial = (ROOT / "src/wreath/_native/geospatial.c").read_text()

    assert '"/std:c11", "/experimental:c11atomics"' in setup
    assert "#define WREATH_PI " in geospatial
    assert "const double to_rad = M_PI" not in geospatial


def test_outbound_client_uses_cpython_portable_monotonic_clock() -> None:
    source = (ROOT / "src/wreath/_native/client_http1.c").read_text()

    assert "PyTime_Monotonic(&now)" in source
    assert "clock_gettime" not in source
    assert "CLOCK_MONOTONIC" not in source


def test_publish_follows_the_exact_successful_main_ci_commit() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    trigger = workflow[True]
    workflow_run = trigger["workflow_run"]
    assert workflow_run == {
        "workflows": ["ci"],
        "types": ["completed"],
        "branches": ["main"],
    }

    jobs = workflow["jobs"]
    assert "verify" not in jobs
    detect = jobs["detect"]
    assert "workflow_run.conclusion == 'success'" in detect["if"]
    assert "workflow_run.event == 'push'" in detect["if"]
    assert "head_repository.full_name == github.repository" in detect["if"]
    detect_checkout = detect["steps"][0]
    assert detect_checkout["with"]["ref"] == (
        "${{ github.event.workflow_run.head_sha || github.sha }}"
    )

    for job_name in ("wheels", "linux-wheels", "http3-wheels", "release"):
        checkout = next(
            step
            for step in jobs[job_name]["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ needs.detect.outputs.sha }}"


def test_workflow_javascript_actions_use_node24_releases() -> None:
    uses: set[str] = set()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        uses |= _action_uses(yaml.safe_load(path.read_text()))

    assert {
        "actions/checkout@v7",
        "actions/download-artifact@v8",
        "actions/setup-python@v7",
        "actions/upload-artifact@v7",
        "astral-sh/setup-uv@v9.0.0",
    } <= uses
    assert (
        not {
            "actions/checkout@v4",
            "actions/configure-pages@v5",
            "actions/deploy-pages@v4",
            "actions/download-artifact@v4",
            "actions/setup-python@v5",
            "actions/upload-artifact@v4",
            "actions/upload-pages-artifact@v3",
            "astral-sh/setup-uv@v5",
        }
        & uses
    )


def test_publish_matrix_covers_native_linux_macos_and_windows() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    matrix = workflow["jobs"]["wheels"]["strategy"]["matrix"]["include"]
    targets = {(entry["runner"], entry["platform"], entry["arch"]) for entry in matrix}
    assert targets == {
        ("ubuntu-latest", "linux", "x86_64"),
        ("ubuntu-24.04-arm", "linux", "aarch64"),
        ("macos-15-intel", "macos", "x86_64"),
        ("macos-15", "macos", "arm64"),
        ("windows-2025", "windows", "AMD64"),
    }

    jobs = workflow["jobs"]
    required = {"wheels", "linux-wheels", "http3-wheels"}
    assert required <= set(jobs["release"]["needs"])
    assert jobs["wheels"]["strategy"]["fail-fast"] is False
    assert jobs["linux-wheels"]["strategy"]["fail-fast"] is False
    assert jobs["http3-wheels"]["strategy"]["fail-fast"] is False
    publish_jobs = {
        "pypi": ("publish", "wheels-base-*"),
        "pypi-linux": ("publish-linux", "wheels-linux-*"),
        "pypi-http3": ("publish-http3", "wheels-http3-*"),
    }
    for job_name, (environment, artifact_pattern) in publish_jobs.items():
        job = jobs[job_name]
        assert required | {"release"} <= set(job["needs"])
        assert job["environment"] == environment
        download = next(
            step for step in job["steps"] if step.get("uses") == "actions/download-artifact@v8"
        )
        assert download["with"]["pattern"] == artifact_pattern

    assert {"pypi-linux", "pypi-http3"} <= set(jobs["pypi"]["needs"])
