"""The migration inventory keeps ownership instead of flattening several apps."""

from __future__ import annotations

import json
import runpy
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from wreath._auth.requirements import requirement_for
from wreath._port.cli import EXIT_WORK_REMAINS, execute
from wreath.authorization import EntityUid
from wreath.port import inventory_projects


def _project(root: Path, name: str, *, python: str = ">=3.14") -> Path:
    project = root / name
    source = project / "src"
    source.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                f'name = "{name}"',
                'version = "0.1.0"',
                f'requires-python = "{python}"',
                'dependencies = ["fastapi", "private-compass"]',
                "",
                "[tool.uv.sources]",
                'private-compass = { index = "private" }',
            )
        )
        + "\n"
    )
    return source


def test_inventory_keeps_same_named_files_attached_to_their_project(tmp_path: Path) -> None:
    first = _project(tmp_path, "llama_trek")
    second = _project(tmp_path, "camera_trap")
    (first / "routes.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        '@router.get("/treks")\nasync def treks(): return []\n'
    )
    (second / "routes.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        '@router.post("/captures")\nasync def capture(): return {}\n'
    )

    report = inventory_projects([first, second])
    document = report.as_dict()

    assert [project["name"] for project in document["projects"]] == ["src-1", "src-2"]
    assert document["route_count"] == 2
    assert document["projects"][0]["routes"][0]["path"] == "/treks"
    assert document["projects"][1]["routes"][0]["path"] == "/captures"


def test_inventory_keeps_a_dynamic_route_path_explicitly_unknown(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "routes.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get()\nasync def captures(): return []\n"
    )

    route = inventory_projects([source]).projects[0].routes[0]

    assert route.path is None


def test_dependency_guard_becomes_a_typed_policy_candidate(tmp_path: Path) -> None:
    source = _project(tmp_path, "llama_trek")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import guard

router = APIRouter()

@router.get("/treks/{trek_id}")
async def trek(
    access=Depends(guard(
        resource={"type": ResourceKind.TREK},
        operation=Operation.READ,
        condition="principal.is_guide",
    )),
):
    return {}
"""
    )

    route = inventory_projects([source]).projects[0].routes[0]
    candidate = route.policies[0]

    assert route.access == "dependency-guarded"
    assert candidate.action_id == "Trek::read"
    assert candidate.conditions == ("principal.is_guide",)
    assert candidate.complete is True
    assert candidate.as_dict()["wreath_decorator"] == (
        "authorize(action='Trek::read', resource={'type': ResourceKind.TREK})"
    )


def test_inventory_generates_compiled_fail_closed_cedar(tmp_path: Path) -> None:
    source = _project(tmp_path, "llama_trek")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import guard

router = APIRouter()

@router.get("/treks/{trek_id}")
async def trek(access=Depends(guard(
    resource={"type": ResourceKind.TREK},
    operation=Operation.READ,
    condition="principal.is_guide",
))):
    return {}
"""
    )

    module = inventory_projects([source]).cedar_module()
    generated = tmp_path / "wreath_port_policies.py"
    generated.write_text(module)
    namespace = runpy.run_path(str(generated))

    policies = namespace["POLICIES"]
    assert len(policies) == 1
    assert "principal.is_guide" in cast(str, namespace["POLICY_SOURCE"])
    assert namespace["REVIEW"] == []

    def handler() -> None:
        pass

    decorator = cast(
        Callable[[object], object],
        next(iter(cast(dict[str, object], namespace["ROUTE_DECORATORS"]).values())),
    )
    guarded = decorator(handler)
    requirement = requirement_for(guarded).policies[0]
    resource_factory = cast(Callable[[object], object], requirement.resource)
    resource = resource_factory(SimpleNamespace(path="/treks/42", path_params={"trek_id": "42"}))
    assert requirement.action == "Trek::read"
    assert resource == EntityUid("Trek", "42")


def test_inventory_generates_a_real_deny_for_an_ambiguous_guard(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import permission_guard

router = APIRouter()

@router.delete("/captures/{capture_id}")
async def discard(access=Depends(permission_guard(
    resource={"type": ResourceKind.CAPTURE},
    operation=Operation.DELETE,
))):
    return None
"""
    )

    module = inventory_projects([source]).cedar_module()
    generated = tmp_path / "wreath_port_policies.py"
    generated.write_text(module)
    namespace = runpy.run_path(str(generated))

    assert cast(str, namespace["POLICY_SOURCE"]).startswith("forbid(")
    review = cast(list[dict[str, object]], namespace["REVIEW"])
    assert review[0]["reason"] == "replace generated default-deny with the intended Cedar grant"


def test_inventory_recovers_static_dependency_factory_defaults(tmp_path: Path) -> None:
    source = _project(tmp_path, "llama_trek")
    (source / "guards.py").write_text(
        """\
def guard(
    resource={"type": ResourceKind.TREK},
    action=Operation.READ,
    condition="",
):
    return None
"""
    )
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import guard

router = APIRouter()

@router.get("/treks/{trek_id}")
async def trek(access=Depends(guard(condition="principal.active"))):
    return {}
"""
    )

    candidate = inventory_projects([source]).projects[0].routes[0].policies[0]

    assert candidate.complete is True
    assert candidate.action_id == "Trek::read"
    assert candidate.condition == "principal.active"


def test_inventory_accepts_generic_temporal_and_hierarchical_semantics(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import permission_guard

router = APIRouter()

@router.get("/captures/{capture_id}")
async def capture(access=Depends(permission_guard(
    resource={"type": ResourceKind.CAPTURE},
    action=Operation.UPDATE,
    condition="principal.is_reviewer",
))):
    return {}
"""
    )

    module = inventory_projects([source]).cedar_module(
        required_condition=(
            "principal.active && context.now < principal.expires_at && principal in resource.scope"
        ),
        action_conditions={
            "update": "!principal.read_only",
        },
        condition_map={"principal.is_reviewer": "principal.reviewer"},
    )
    generated = tmp_path / "authorization.py"
    generated.write_text(module)
    namespace = runpy.run_path(str(generated))

    source_text = cast(str, namespace["POLICY_SOURCE"])
    assert "principal.active" in source_text
    assert "context.now < principal.expires_at" in source_text
    assert "principal in resource.scope" in source_text
    assert "!principal.read_only" in source_text
    assert "principal.reviewer" in source_text
    assert source_text.startswith("permit(")
    assert namespace["REVIEW"] == []


def test_authentication_only_factories_do_not_become_cedar_policies(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from security import AuthenticationVerifier

router = APIRouter()

@router.get("/captures")
async def captures(access=Depends(AuthenticationVerifier("v1"))):
    return []
"""
    )

    module = inventory_projects([source]).cedar_module(
        authentication_factories={"AuthenticationVerifier"}
    )
    generated = tmp_path / "authorization.py"
    generated.write_text(module)
    namespace = runpy.run_path(str(generated))

    assert namespace["ROUTE_DECORATORS"] == {}
    assert namespace["REVIEW"] == []


def test_an_inventory_without_policy_candidates_still_compiles_default_deny(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/captures")\n'
        "async def captures(): return []\n"
    )

    generated = tmp_path / "default_deny_authorization.py"
    generated.write_text(inventory_projects([source]).cedar_module())
    namespace = runpy.run_path(str(generated))

    assert namespace["POLICY_SOURCE"] == "forbid(principal, action, resource);\n"
    assert "permit(" not in str(namespace["POLICY_SOURCE"])
    assert namespace["ROUTE_DECORATORS"] == {}


def test_inventory_refuses_an_unknown_migration_strategy(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap")
    with pytest.raises(ValueError, match="preserve.*baseline"):
        inventory_projects([source], migration_strategy="guess")


def test_incomplete_policy_candidates_remain_explicit_review_records(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import guard

router = APIRouter()

@router.get("/captures")
async def captures(access=Depends(guard(action=operation_for_request()))):
    return []
"""
    )

    generated = tmp_path / "authorization.py"
    generated.write_text(inventory_projects([source]).cedar_module())
    namespace = runpy.run_path(str(generated))

    assert namespace["ROUTE_DECORATORS"] == {}
    assert namespace["REVIEW"][0]["reason"] == "action or resource type was not static"


def test_required_policy_does_not_erase_an_unknown_explicit_condition(tmp_path: Path) -> None:
    source = _project(tmp_path, "llama_trek")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import guard

router = APIRouter()

@router.get("/treks/{trek_id}")
async def trek(access=Depends(guard(
    resource={"type": ResourceKind.TREK},
    action=Operation.READ,
    condition="lookup_permission(principal)",
))):
    return {}
"""
    )

    module = inventory_projects([source]).cedar_module(
        required_condition="principal.active && principal in resource.scope"
    )
    generated = tmp_path / "authorization.py"
    generated.write_text(module)
    namespace = runpy.run_path(str(generated))

    assert namespace["POLICY_SOURCE"].startswith("forbid(")
    assert len(namespace["REVIEW"]) == 1


def test_python_and_private_source_audit_are_explicit(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap", python=">=3.10,<3.11")
    (source / "app.py").write_text("VALUE = 1\n")

    audit = inventory_projects([source]).projects[0].dependencies

    assert audit["target_status"] == "project-blocked"
    dependencies = {item["name"]: item for item in audit["dependencies"]}
    assert dependencies["fastapi"]["replacement"] == "wreath"
    assert dependencies["private-compass"]["source"] == "index"
    assert dependencies["private-compass"]["python_compatibility"] == "unverified"


def test_python_audit_understands_strict_lower_bounds(tmp_path: Path) -> None:
    source = _project(tmp_path, "camera_trap", python=">3.14")
    (source / "app.py").write_text("VALUE = 1\n")

    audit = inventory_projects([source]).projects[0].dependencies

    assert audit["target_status"] == "project-blocked"


def test_inventory_write_and_check_are_byte_stable(tmp_path: Path) -> None:
    source = _project(tmp_path, "llama_trek")
    (source / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    inventory = tmp_path / "port-inventory.json"
    base = dict(
        source=[str(source)],
        inventory=True,
        target_python="3.14",
        migration_strategy="preserve",
        as_json=False,
        by_rule=False,
        rule=None,
        context=0,
        in_place=False,
        output=None,
        force=False,
        opinionated=False,
        write_inventory=None,
        check_inventory=None,
        write_cedar=None,
        cedar_semantics=None,
    )

    assert execute(Namespace(**(base | {"write_inventory": str(inventory)}))) == 0
    assert json.loads(inventory.read_text())["format"] == "wreath-port-inventory-1"
    assert execute(Namespace(**(base | {"check_inventory": str(inventory)}))) == 0

    inventory.write_text("{}\n")
    assert execute(Namespace(**(base | {"check_inventory": str(inventory)}))) == EXIT_WORK_REMAINS


def test_inventory_writes_an_importable_cedar_module(tmp_path: Path) -> None:
    source = _project(tmp_path, "llama_trek")
    (source / "routes.py").write_text(
        """\
from fastapi import APIRouter, Depends
from guards import guard

router = APIRouter()

@router.get("/treks/{trek_id}")
async def trek(access=Depends(guard(
    resource={"type": ResourceKind.TREK},
    operation=Operation.READ,
    condition="principal.is_guide",
))):
    return {}
"""
    )
    target = tmp_path / "ported" / "authorization.py"
    semantics = tmp_path / "cedar-semantics.json"
    semantics.write_text(
        json.dumps(
            {
                "required_condition": "principal.active && principal in resource.scope",
                "conditions": {"principal.is_guide": "principal.active"},
                "authentication_factories": ["TokenVerifier"],
            }
        )
    )
    namespace = Namespace(
        source=[str(source)],
        inventory=True,
        target_python="3.14",
        migration_strategy="preserve",
        as_json=False,
        by_rule=False,
        rule=None,
        context=0,
        in_place=False,
        output=None,
        force=False,
        opinionated=False,
        write_inventory=None,
        check_inventory=None,
        write_cedar=str(target),
        cedar_semantics=str(semantics),
    )

    assert execute(namespace) == 0
    generated = runpy.run_path(str(target))
    assert len(generated["POLICIES"]) == 1
    assert "principal.active" in generated["POLICY_SOURCE"]
    assert "principal in resource.scope" in generated["POLICY_SOURCE"]


def test_baseline_strategy_counts_historical_migration_sites_as_retired(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path, "camera_trap")
    (source / "migration.py").write_text(
        "from alembic import op\nop.execute('update sightings set reviewed = true')\n"
    )

    document = inventory_projects([source], migration_strategy="baseline").as_dict()
    project = document["projects"][0]

    assert project["analysis"]["baseline_retired_findings"] == 1
    assert document["counts"]["unsupported"] == 0
    assert document["counts"]["retired"] == 1
