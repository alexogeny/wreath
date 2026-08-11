"""The published distributions and native-runner matrix stay one contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _toml(path: str) -> dict:
    with (ROOT / path).open("rb") as stream:
        return tomllib.load(stream)


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
    assert linux["tool"]["cibuildwheel"]["test-command"].endswith(
        "wheel_smoke.py --reactor"
    )
    assert http3["tool"]["cibuildwheel"]["test-command"].endswith(
        "wheel_smoke.py --http3"
    )


def test_base_build_cleans_stale_capability_outputs() -> None:
    setup = (ROOT / "setup.py").read_text()
    assert "class _CleanBuild(build):" in setup
    assert 'cmdclass={"build": _CleanBuild' in setup


def test_publish_matrix_covers_native_linux_macos_and_windows() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    matrix = workflow["jobs"]["wheels"]["strategy"]["matrix"]["include"]
    targets = {
        (entry["runner"], entry["platform"], entry["arch"])
        for entry in matrix
    }
    assert targets == {
        ("ubuntu-latest", "linux", "x86_64"),
        ("ubuntu-24.04-arm", "linux", "aarch64"),
        ("macos-15-intel", "macos", "x86_64"),
        ("macos-15", "macos", "arm64"),
        ("windows-2025", "windows", "AMD64"),
    }

    jobs = workflow["jobs"]
    required = {"verify", "wheels", "linux-wheels", "http3-wheels"}
    assert required <= set(jobs["release"]["needs"])
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
            step
            for step in job["steps"]
            if step.get("uses") == "actions/download-artifact@v4"
        )
        assert download["with"]["pattern"] == artifact_pattern

    assert {"pypi-linux", "pypi-http3"} <= set(jobs["pypi"]["needs"])
