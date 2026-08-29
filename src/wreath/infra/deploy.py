"""Inspectable deployment artifacts from an inferred infrastructure plan.

This renderer accepts an immutable OCI image and produces an inert Compose
bundle. It never contacts a provider and deliberately has no apply operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .model import InfrastructurePlan, as_dict

_DIGEST_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"[^a-z0-9_-]+")
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class DeploymentArtifact:
    """One named UTF-8 file in a deployment bundle."""

    path: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str):
            raise TypeError(
                "deployment artifact path must be a relative string beneath the bundle root"
            )
        candidate = Path(self.path)
        if (
            not self.path
            or "\x00" in self.path
            or candidate.is_absolute()
            or candidate == Path(".")
            or ".." in candidate.parts
        ):
            raise ValueError(
                f"deployment artifact path {self.path!r} is invalid; use a relative "
                "file path without '.' or '..' beneath the bundle root"
            )


@dataclass(frozen=True, slots=True)
class DeploymentBundle:
    """A complete, inert set of deployment files."""

    artifacts: tuple[DeploymentArtifact, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for artifact in self.artifacts:
            if not isinstance(artifact, DeploymentArtifact):
                raise TypeError("deployment bundle artifacts must be DeploymentArtifact values")
            if artifact.path in seen:
                raise ValueError(
                    f"duplicate deployment artifact path {artifact.path!r}; use one "
                    "artifact per relative file path"
                )
            seen.add(artifact.path)

    def files(self) -> dict[str, str]:
        """Return the bundle as `{relative path: content}`."""
        return {artifact.path: artifact.content for artifact in self.artifacts}

    def write(self, directory: str | Path, *, force: bool = False) -> tuple[Path, ...]:
        """Write each artifact atomically beneath one explicit directory.

        Existing bundle files are refused unless `force=True`. Unrelated
        files in the directory are neither read nor removed.
        """
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve()
        targets = tuple(root / artifact.path for artifact in self.artifacts)
        escaped = next(
            (
                (artifact, target)
                for artifact, target in zip(self.artifacts, targets, strict=True)
                if not target.resolve(strict=False).is_relative_to(resolved_root)
            ),
            None,
        )
        if escaped is not None:
            artifact, target = escaped
            raise ValueError(
                f"deployment artifact path {artifact.path!r} resolves outside bundle "
                f"root {resolved_root}; remove the escaping symlink at {target.parent}"
            )
        existing = [target for target in targets if target.exists()]
        if existing and not force:
            names = ", ".join(str(path) for path in existing)
            raise ValueError(
                f"deployment bundle would overwrite {names}; use force=True only "
                "after reviewing the existing artifacts"
            )
        written: list[Path] = []
        for artifact, target in zip(self.artifacts, targets, strict=True):
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            temporary.write_text(artifact.content, encoding="utf-8")
            temporary.replace(target)
            written.append(target)
        return tuple(written)


def _json_line(value: object) -> str:
    """A JSON scalar or collection, also valid YAML 1.2."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _volume_name(service: str, store: str) -> str:
    normalized = _SAFE_NAME.sub("-", f"{service}-{store}".lower()).strip("-")
    return normalized or "wreath-data"


def _compose(
    plan: InfrastructurePlan,
    *,
    image: str,
    service: str,
    port: int,
    factory: bool,
) -> str:
    command = [
        "wreath",
        "run",
        plan.application,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
    ]
    if factory:
        command.append("--factory")
    required_keys = tuple(
        sorted({key.key for contract in plan.settings for key in contract.keys if key.required})
    )
    local_stores = tuple(store for store in plan.object_stores if store.backend == "local")
    published_port = _json_line(f"{port}:{port}")
    temporary_path = _json_line("/tmp")
    lines = [
        "# Generated from wreath.infra; inspect before deployment.",
        "services:",
        f"  {service}:",
        f"    image: {_json_line(image)}",
        f"    command: {_json_line(command)}",
        "    restart: unless-stopped",
        "    read_only: true",
        "    init: true",
        f"    ports: [{published_port}]",
        f"    tmpfs: [{temporary_path}]",
    ]
    if required_keys:
        lines.append("    environment:")
        for key in required_keys:
            expression = "${" + key + ":?" + key + " is required}"
            lines.append(f"      {key}: {_json_line(expression)}")
    if local_stores:
        lines.append("    volumes:")
        for store in local_stores:
            if store.root is None:
                raise ValueError(
                    f"local object store {store.name!r} has no root; declare root=PATH"
                )
            lines.extend(
                (
                    "      - type: volume",
                    f"        source: {_volume_name(service, store.name)}",
                    f"        target: {_json_line(store.root)}",
                )
            )
        lines.append("volumes:")
        for store in local_stores:
            lines.append(f"  {_volume_name(service, store.name)}: {{}}")
    return "\n".join(lines) + "\n"


def deployment_bundle(
    plan: InfrastructurePlan,
    *,
    image: str,
    service: str = "wreath-app",
    port: int = 8000,
    factory: bool = False,
) -> DeploymentBundle:
    """Build a provider-neutral Compose deployment bundle.

    `image` must carry a SHA-256 digest. A mutable tag can resolve to a
    different application between review and rollout, so accepting one would
    make the checksummed bundle reproducible in name only. Plans with gaps are
    refused before any file is produced. A local object-store root must be an
    absolute path naming its location inside the container.
    """
    if plan.gaps:
        subjects = ", ".join(gap.subject for gap in plan.gaps)
        raise ValueError(
            f"deployment bundle requires a gap-free infrastructure plan; resolve {subjects}"
        )
    if not _DIGEST_IMAGE.fullmatch(image):
        raise ValueError(
            "deployment image must be an immutable OCI reference ending in "
            "@sha256:<64 lowercase hex digits>"
        )
    if not _SERVICE_NAME.fullmatch(service):
        raise ValueError(
            "deployment service name must start with a lowercase letter or digit "
            "and contain only lowercase letters, digits, '_' or '-'"
        )
    if not 1 <= port <= 65535:
        raise ValueError("deployment port must be between 1 and 65535")

    local_stores = tuple(store for store in plan.object_stores if store.backend == "local")
    volume_names = tuple(_volume_name(service, store.name) for store in local_stores)
    if len(set(volume_names)) != len(volume_names):
        raise ValueError(
            "local object store names collide after deployment volume normalization; "
            "use distinct letters, digits, '_' or '-' in each store name"
        )
    roots = tuple(store.root for store in local_stores if store.root is not None)
    if len(roots) != len(local_stores):
        missing = next(store.name for store in local_stores if store.root is None)
        raise ValueError(f"local object store {missing!r} has no root; declare root=PATH")
    relative = next((root for root in roots if not Path(root).is_absolute()), None)
    if relative is not None:
        raise ValueError(
            f"local object store root {relative!r} must be absolute for deployment; "
            "use an absolute container path in app.objects(..., root=PATH)"
        )
    if len(set(roots)) != len(roots):
        raise ValueError(
            "local object stores must use distinct roots in a deployment bundle; "
            "one container path cannot have two volume owners"
        )

    compose = _compose(plan, image=image, service=service, port=port, factory=factory)
    plan_json = json.dumps(as_dict(plan), indent=2) + "\n"
    contract = (
        json.dumps(
            {
                "format": "wreath.deployment.v1",
                "application": plan.application,
                "image": image,
                "service": service,
                "port": port,
                "factory": factory,
                "required_environment": sorted(
                    {key.key for settings in plan.settings for key in settings.keys if key.required}
                ),
                "persistent_paths": sorted(
                    store.root
                    for store in plan.object_stores
                    if store.backend == "local" and store.root is not None
                ),
                "egress_origins": sorted(rule.origin for rule in plan.egress),
            },
            indent=2,
        )
        + "\n"
    )
    payloads = (
        DeploymentArtifact("compose.yaml", compose),
        DeploymentArtifact("infrastructure-plan.json", plan_json),
        DeploymentArtifact("deployment.json", contract),
    )
    checksums = "".join(
        f"{hashlib.sha256(artifact.content.encode()).hexdigest()}  {artifact.path}\n"
        for artifact in payloads
    )
    return DeploymentBundle((*payloads, DeploymentArtifact("SHA256SUMS", checksums)))


__all__ = ["DeploymentArtifact", "DeploymentBundle", "deployment_bundle"]
