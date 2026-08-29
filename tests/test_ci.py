from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from wreath._ci import FORGES, CiPlan, plan, render
from wreath._cli import main


def _new(target: Path, *args: str) -> int:
    return main(["new", target.name, "--directory", str(target.parent), *args])


@pytest.mark.parametrize("forge", FORGES)
def test_every_forge_renders_files_that_parse_as_yaml(forge: str) -> None:
    for path, content in render(plan("shop"), forge).items():
        document = yaml.safe_load(content)
        assert isinstance(document, dict), f"{path} did not parse as a mapping"


@pytest.mark.parametrize("forge", FORGES)
def test_every_forge_carries_every_command_in_the_plan(forge: str) -> None:
    ci = plan("shop")
    rendered = "\n".join(render(ci, forge).values())
    missing = [command for command in ci.commands() if command not in rendered]
    assert missing == [], f"{forge} lost: {missing}"


@pytest.mark.parametrize("forge", FORGES)
def test_every_forge_installs_before_it_runs_anything(forge: str) -> None:
    ci = plan("shop")
    for path, content in render(ci, forge).items():
        for command in ci.commands():
            assert content.index("uv sync") < content.index(command), (
                f"{path} runs {command!r} before installing"
            )


def test_the_preflight_target_names_the_project_being_generated() -> None:
    assert any(
        "wreath doctor preflight shop.app:app" in command for command in plan("shop").commands()
    )
    assert not any("myapp.app:app" in command for command in plan("shop").commands())


def test_the_plan_typechecks_the_generated_package() -> None:
    assert "uv run ty check" in plan("shop").commands()


def test_the_generated_github_pipeline_uses_node24_actions() -> None:
    workflow = render(plan("shop"), "github")[".github/workflows/ci.yml"]
    assert "uses: actions/checkout@v7" in workflow
    assert "uses: astral-sh/setup-uv@v9.0.0" in workflow
    assert "uses: actions/checkout@v4" not in workflow
    assert "uses: astral-sh/setup-uv@v5" not in workflow


def test_the_env_file_is_copied_before_anything_imports_the_application() -> None:
    for check in plan("shop").checks:
        imports = [c for c in check.commands if "pytest" in c or "preflight" in c]
        if imports:
            assert check.commands[0] == "cp .env.example .env"


def test_an_unknown_forge_is_refused_by_name_and_lists_the_real_ones() -> None:
    with pytest.raises(ValueError) as raised:
        render(plan("shop"), "bitbucket")
    assert "bitbucket" in str(raised.value)
    assert "github" in str(raised.value)


def test_codeberg_and_forgejo_are_one_rendering_under_two_names() -> None:
    assert render(plan("shop"), "codeberg") == render(plan("shop"), "forgejo")


def test_forgejo_and_gitea_name_their_action_host_outright() -> None:
    forgejo = render(plan("shop"), "codeberg")[".forgejo/workflows/ci.yml"]
    gitea = render(plan("shop"), "gitea")[".gitea/workflows/ci.yml"]
    assert "https://code.forgejo.org/actions/checkout@" in forgejo
    assert "https://github.com/actions/checkout@" in gitea
    for content in (forgejo, gitea):
        assert "uses: actions/checkout@" not in content


@pytest.mark.parametrize(
    ("forge", "path"),
    [
        ("github", ".github/workflows/ci.yml"),
        ("gitlab", ".gitlab-ci.yml"),
        ("codeberg", ".forgejo/workflows/ci.yml"),
        ("gitea", ".gitea/workflows/ci.yml"),
    ],
)
def test_new_writes_the_file_the_forge_actually_reads(
    tmp_path: Path,
    forge: str,
    path: str,
) -> None:
    target = tmp_path / "shop"
    assert _new(target, "--forge", forge) == 0
    assert (target / path).is_file()


def test_without_the_flag_no_pipeline_is_written(tmp_path: Path) -> None:
    target = tmp_path / "bare"
    assert _new(target) == 0
    assert list(target.rglob("*.yml")) == []
    assert not (target / ".gitlab-ci.yml").exists()


def test_the_github_pipeline_runs_every_check_as_its_own_job(tmp_path: Path) -> None:
    target = tmp_path / "shop"
    assert _new(target, "--forge", "github") == 0
    document = yaml.safe_load((target / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert set(document["jobs"]) == {check.key for check in plan("shop").checks}
    for job in document["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"
        assert job["steps"]


def test_the_gitlab_pipeline_shares_one_install_across_its_jobs(tmp_path: Path) -> None:
    target = tmp_path / "shop"
    assert _new(target, "--forge", "gitlab") == 0
    document = yaml.safe_load((target / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert "uv sync" in document["default"]["before_script"]
    jobs = [key for key in document if key not in ("default", "stages")]
    assert sorted(jobs) == sorted(check.key for check in plan("shop").checks)
    for name in jobs:
        assert "uv sync" not in document[name]["script"]


@pytest.mark.parametrize("profile", ["service", "modular-monolith"])
def test_the_generated_project_lints_clean_under_its_own_ruff_config(
    tmp_path: Path,
    profile: str,
) -> None:
    import subprocess
    import sys

    target = tmp_path / profile.replace("-", "_")
    assert _new(target, "--forge", "github", "--profile", profile) == 0
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 2:  # ruff itself failed to run, not a lint finding
        pytest.fail(f"could not run ruff: {result.stderr}")
    assert result.returncode == 0, result.stdout + result.stderr


def _project(directory: Path, name: str = "shop") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    return directory


def test_ci_init_writes_the_pipeline_into_an_existing_project(tmp_path: Path) -> None:
    project = _project(tmp_path / "existing")
    assert main(["ci", "init", "--forge", "github", "--directory", str(project)]) == 0
    assert (project / ".github/workflows/ci.yml").is_file()


def test_ci_init_writes_every_forge_it_was_given(tmp_path: Path) -> None:
    project = _project(tmp_path / "mirrored")
    assert (
        main(
            [
                "ci",
                "init",
                "--forge",
                "github",
                "--forge",
                "codeberg",
                "--directory",
                str(project),
            ]
        )
        == 0
    )
    assert (project / ".github/workflows/ci.yml").is_file()
    assert (project / ".forgejo/workflows/ci.yml").is_file()


def test_ci_init_reads_the_package_name_from_pyproject(tmp_path: Path) -> None:
    project = _project(tmp_path / "some-checkout-dir", name="shop")
    assert main(["ci", "init", "--forge", "github", "--directory", str(project)]) == 0
    content = (project / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "shop.app:app" in content
    assert "some-checkout-dir" not in content


def test_ci_init_turns_a_distribution_name_into_an_importable_one(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "hyphenated", name="shop-api")
    assert main(["ci", "init", "--forge", "github", "--directory", str(project)]) == 0
    content = (project / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "shop_api.app:app" in content


def test_ci_init_refuses_to_write_over_a_pipeline_somebody_relies_on(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "hasci")
    workflow = project / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: mine\n", encoding="utf-8")
    assert main(["ci", "init", "--forge", "github", "--directory", str(project)]) == 2
    assert workflow.read_text(encoding="utf-8") == "name: mine\n"
    assert ".github/workflows/ci.yml" in capsys.readouterr().err


def test_a_collision_on_one_forge_writes_neither(tmp_path: Path) -> None:
    project = _project(tmp_path / "partial")
    (project / ".gitlab-ci.yml").write_text("mine\n", encoding="utf-8")
    assert (
        main(
            [
                "ci",
                "init",
                "--forge",
                "github",
                "--forge",
                "gitlab",
                "--directory",
                str(project),
            ]
        )
        == 2
    )
    assert not (project / ".github/workflows/ci.yml").exists()


def test_ci_init_without_a_pyproject_asks_for_the_name_rather_than_guessing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare = tmp_path / "nomanifest"
    bare.mkdir()
    assert main(["ci", "init", "--forge", "github", "--directory", str(bare)]) == 2
    assert "--name" in capsys.readouterr().err


def test_ci_init_takes_an_explicit_name_over_the_manifest(tmp_path: Path) -> None:
    project = _project(tmp_path / "override", name="shop")
    assert (
        main(
            [
                "ci",
                "init",
                "--forge",
                "github",
                "--directory",
                str(project),
                "--name",
                "warehouse",
            ]
        )
        == 0
    )
    content = (project / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "warehouse.app:app" in content


def test_the_json_summary_lists_every_file_written(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "asjson")
    assert (
        main(
            [
                "ci",
                "init",
                "--forge",
                "gitlab",
                "--forge",
                "github",
                "--directory",
                str(project),
                "--json",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["project"] == "shop"
    assert summary["files"] == [".github/workflows/ci.yml", ".gitlab-ci.yml"]
    for relative in summary["files"]:
        assert (project / relative).is_file()


def test_the_plan_is_built_before_a_forge_is_chosen() -> None:
    ci = plan("shop")
    assert isinstance(ci, CiPlan)
    rendered_words = ("runs-on", "before_script", "uses:", "container")
    for command in ci.commands():
        assert not any(word in command for word in rendered_words)
