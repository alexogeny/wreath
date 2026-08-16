"""Canonical parsing and importing for every `module:attribute` target."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Target:
    module: str
    attribute: str


def parse_target(
    spec: str, *, label: str, default_attribute: str | None = None
) -> Target:
    """Parse one import target, refusing malformed names before importing."""
    if spec.count(":") > 1:
        raise ValueError(
            f"{label} target {spec!r} must be spelled module:attribute"
        )
    module, separator, attribute = spec.partition(":")
    if not separator and default_attribute is not None:
        attribute = default_attribute
    if (
        not module
        or module.startswith(".")
        or any(not part.isidentifier() for part in module.split("."))
        or not attribute
        or not attribute.isidentifier()
    ):
        raise ValueError(
            f"{label} target {spec!r} must be spelled module:attribute"
        )
    return Target(module, attribute)


def load_target(
    spec: str,
    *,
    label: str,
    default_attribute: str | None = None,
    catch_all_import_errors: bool = False,
) -> Any:
    """Import one parsed target and return its selected object."""
    target = parse_target(
        spec, label=label, default_attribute=default_attribute
    )
    try:
        module = importlib.import_module(target.module)
    except Exception as error:
        if not catch_all_import_errors and not isinstance(error, ImportError):
            raise
        raise ValueError(
            f"could not import {label} module {target.module!r}: {error}"
        ) from error
    try:
        return getattr(module, target.attribute)
    except AttributeError as error:
        raise ValueError(
            f"{label} module {target.module!r} has no attribute "
            f"{target.attribute!r}"
        ) from error
