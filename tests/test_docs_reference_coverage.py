from __future__ import annotations

import argparse
import importlib
import inspect
import re
import tomllib
from pathlib import Path

import wreath
from wreath._cli import build_parser
from wreath._docs.apidoc import _module_members
from wreath.policy import HttpPolicy

ROOT = Path(__file__).parents[1]
DIRECTIVE = re.compile(r"^:::\s+(wreath(?:\.[A-Za-z_][A-Za-z0-9_]*)+)$", re.MULTILINE)
POLICY_FIELD = re.compile(r"^\|[^|]+\| `([^`]+)` \| `[^`]+` \|$", re.MULTILINE)
COMMAND_ROW = re.compile(r"^\| `([a-z][a-z0-9-]*)` \| [^|]+\|$", re.MULTILINE)
MIGRATION_COMMAND = re.compile(r"\bmigrations ([a-z][a-z0-9-]*)\b")
DOCUMENTATION_URL = "https://alexogeny.github.io/wreath/"


def public_modules() -> set[str]:
    package = Path(wreath.__file__).parent
    modules = set()
    for source in package.rglob("*.py"):
        relative = source.relative_to(package)
        if source.name == "__init__.py" or any(part.startswith("_") for part in relative.parts):
            continue
        module_name = "wreath." + ".".join(relative.with_suffix("").parts)
        module = importlib.import_module(module_name)
        if _module_members(module):
            modules.add(module_name)
    return modules


def documented_modules() -> set[str]:
    reference = ROOT / "docs" / "reference"
    return {
        target for page in reference.glob("*.md") for target in DIRECTIVE.findall(page.read_text())
    }


def subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if (
            isinstance(choices, dict)
            and choices
            and all(
                isinstance(candidate, argparse.ArgumentParser) for candidate in choices.values()
            )
        ):
            return choices
    raise AssertionError(f"{parser.prog} has no subcommands")


def test_every_public_module_has_generated_member_reference() -> None:
    missing = public_modules() - documented_modules()
    assert not missing, "public modules missing from reference docs: " + ", ".join(sorted(missing))


def test_the_middleware_vocabulary_names_every_http_policy_component() -> None:
    expected = set(inspect.signature(HttpPolicy).parameters)
    page = (ROOT / "docs" / "guides" / "policy.md").read_text()
    documented = set(POLICY_FIELD.findall(page))
    missing = expected - documented
    assert not missing, "HTTP policy components missing from the guide: " + ", ".join(
        sorted(missing)
    )


def test_package_metadata_and_readme_publish_the_documentation_url() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["urls"]["Documentation"] == DOCUMENTATION_URL
    readme = (ROOT / "README.md").read_text()
    assert f"[Documentation]({DOCUMENTATION_URL})" in readme


def test_documentation_names_the_package_version() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    releases = (ROOT / "docs" / "start" / "releases.md").read_text()
    home = (ROOT / "docs" / "index.md").read_text()
    assert f"**Current documentation version: `{version}`.**" in releases
    assert f"Wreath {version} · Python 3.14 · ASGI" in home


def test_command_guide_maps_every_top_level_command() -> None:
    expected = set(subcommands(build_parser()))
    guide = (ROOT / "docs" / "guides" / "cli.md").read_text()
    documented = set(COMMAND_ROW.findall(guide))
    assert documented == expected


def test_migration_guide_covers_every_cli_action() -> None:
    migration_parser = subcommands(build_parser())["migrations"]
    expected = set(subcommands(migration_parser))
    guide = (ROOT / "docs" / "guides" / "migration-workflow.md").read_text()
    documented = set(MIGRATION_COMMAND.findall(guide))
    missing = expected - documented
    assert not missing, "migration actions missing from the guide: " + ", ".join(sorted(missing))
