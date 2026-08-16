from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from wreath import Wreath
from wreath.infra import (
    DeploymentArtifact,
    DeploymentBundle,
    Gap,
    GapKind,
    deployment_bundle,
    infer,
)

IMAGE = "registry.example/wreath/app@sha256:" + "a" * 64


def _plan(tmp_path):
    app = Wreath()
    app.objects("media", backend="local", root=tmp_path / "media")
    app.http_client("payments", base_url="https://payments.example")
    return infer(app, application="service.app:app")


def test_bundle_pins_the_image_and_carries_storage_and_egress_contracts(tmp_path) -> None:
    bundle = deployment_bundle(_plan(tmp_path), image=IMAGE, service="service")
    files = bundle.files()
    assert set(files) == {
        "compose.yaml",
        "deployment.json",
        "infrastructure-plan.json",
        "SHA256SUMS",
    }
    assert f'image: "{IMAGE}"' in files["compose.yaml"]
    assert "source: service-media" in files["compose.yaml"]
    assert f'target: "{tmp_path / "media"}"' in files["compose.yaml"]
    contract = json.loads(files["deployment.json"])
    assert contract["format"] == "wreath.deployment.v1"
    assert contract["egress_origins"] == ["https://payments.example"]
    for line in files["SHA256SUMS"].splitlines():
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256(files[name].encode()).hexdigest()


def test_bundle_refuses_mutable_images_and_unresolved_gaps(tmp_path) -> None:
    plan = _plan(tmp_path)
    with pytest.raises(ValueError, match="immutable OCI reference"):
        deployment_bundle(plan, image="registry.example/wreath/app:latest")
    blocked = replace(
        plan,
        gaps=(Gap(GapKind.UNDERIVABLE, "webhook tables", "choose a database"),),
    )
    with pytest.raises(ValueError, match="resolve webhook tables"):
        deployment_bundle(blocked, image=IMAGE)


def test_bundle_refuses_ambiguous_volume_ownership(tmp_path) -> None:
    app = Wreath()
    app.objects("one", backend="local", root=tmp_path / "shared")
    app.objects("two", backend="local", root=tmp_path / "shared")
    plan = infer(app, application="service.app:app")
    with pytest.raises(ValueError, match="distinct roots"):
        deployment_bundle(plan, image=IMAGE)


def test_bundle_refuses_a_relative_persistent_container_path(tmp_path) -> None:
    plan = _plan(tmp_path)
    relative_store = replace(plan.object_stores[0], root="relative-media")
    plan = replace(plan, object_stores=(relative_store,))
    with pytest.raises(ValueError, match="must be absolute for deployment"):
        deployment_bundle(plan, image=IMAGE)


@pytest.mark.parametrize("service", ("-service", "Service", "service.name"))
def test_bundle_refuses_invalid_compose_service_names(tmp_path, service) -> None:
    with pytest.raises(ValueError, match="must start with"):
        deployment_bundle(_plan(tmp_path), image=IMAGE, service=service)


def test_bundle_write_refuses_to_clobber_reviewed_artifacts(tmp_path) -> None:
    bundle = deployment_bundle(_plan(tmp_path), image=IMAGE)
    output = tmp_path / "deploy"
    written = bundle.write(output)
    assert len(written) == 4
    with pytest.raises(ValueError, match="would overwrite"):
        bundle.write(output)


@pytest.mark.parametrize(
    "path",
    ("", ".", "..", "../outside", "nested/../../outside", "bad\x00name"),
)
def test_deployment_artifact_refuses_paths_outside_the_bundle(path) -> None:
    with pytest.raises(ValueError, match="use a relative file path"):
        DeploymentArtifact(path, "content")


def test_deployment_artifact_refuses_an_absolute_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="use a relative file path"):
        DeploymentArtifact(str(tmp_path / "outside"), "content")


def test_deployment_bundle_refuses_duplicate_paths() -> None:
    artifact = DeploymentArtifact("compose.yaml", "content")
    with pytest.raises(ValueError, match="duplicate deployment artifact path"):
        DeploymentBundle((artifact, artifact))


def test_bundle_write_refuses_a_symlink_escape(tmp_path) -> None:
    root = tmp_path / "bundle"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    bundle = DeploymentBundle((DeploymentArtifact("linked/escaped.txt", "secret"),))
    with pytest.raises(ValueError, match="resolves outside bundle root"):
        bundle.write(root)
    assert not (outside / "escaped.txt").exists()
