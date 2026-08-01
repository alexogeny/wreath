"""The environment keys a settings dataclass reads, derived the way `bind` does.

`wreath.config.Environment.bind` turns a dataclass into an environment contract
by a fixed rule: uppercase the field name, join it to the prefix with `_` at the
root and `__` inside a nested group, and let `Annotated[T, Env("EXACT")]`
override the whole thing. That rule is the app-side half of the seam this module
exists to close, so the keys are derived from the *same* primitives the binder
uses rather than from a second reading of the same idea.

Concretely, `_unwrap_env` is imported from `wreath.config` instead of being
reimplemented. A copy would be a second place for the `Annotated`/`Env` rule to
live, and the failure mode of two copies is not a crash -- it is a plan that
names a key the application never reads, which is exactly the class of defect
this whole feature is for.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any

from ..config import Secret, _unwrap_env
from .model import SettingsKey

__all__ = ["describe_annotation", "settings_keys"]

_MISSING = dataclasses.MISSING


def describe_annotation(annotation: Any) -> str:
    """A short readable name for a settings annotation.

    Used for display only. `Secret[str]` renders as `Secret[str]` rather than as
    the wrapped type, because a reader deciding how to supply a key needs to
    know it is a credential.
    """
    origin = typing.get_origin(annotation)
    if origin is Secret:
        inner = typing.get_args(annotation)[0]
        return f"Secret[{describe_annotation(inner)}]"
    if isinstance(annotation, type):
        return annotation.__name__
    text = str(annotation)
    return text.removeprefix("typing.")


def _is_dataclass_type(annotation: Any) -> bool:
    return dataclasses.is_dataclass(annotation) and isinstance(annotation, type)


def settings_keys(settings: type, *, prefix: str = "") -> tuple[SettingsKey, ...]:
    """Every environment key `Environment.bind(settings, prefix=prefix)` will read.

    Nested dataclass fields are walked exactly as the binder walks them, so
    `database.host` under `prefix="APP"` comes back as `APP_DATABASE__HOST` with
    the dotted field path preserved for the report.

    `supplied_by` is left `None` here; deciding what supplies a key is the
    caller's job, and keeping the two apart is what lets one derivation be
    checked against several environments.

    Args:
        settings: the dataclass type an application binds.
        prefix: the same `prefix` that would be passed to `bind`.

    Returns:
        One `SettingsKey` per leaf field, in declaration order.

    Raises:
        TypeError: `settings` is not a dataclass type, or an annotation on it
            cannot be resolved -- the same two refusals `bind` makes, for the
            same reason.
    """
    if not _is_dataclass_type(settings):
        raise TypeError("settings must be a dataclass type")
    return tuple(_walk(settings, prefix.rstrip("_"), "", nested=False))


def _walk(
    settings: type, prefix: str, path: str, *, nested: bool
) -> list[SettingsKey]:
    try:
        hints = typing.get_type_hints(settings, include_extras=True)
    except NameError as error:
        raise TypeError(
            f"settings model {settings.__qualname__} has an unresolvable annotation: "
            f"{error}"
        ) from error
    found: list[SettingsKey] = []
    for item in dataclasses.fields(settings):
        annotation = hints.get(item.name, Any)
        base, marker = _unwrap_env(annotation)
        separator = "__" if nested else "_"
        conventional = (
            f"{prefix}{separator}{item.name.upper()}" if prefix else item.name.upper()
        )
        key = marker.name if marker is not None else conventional
        dotted = f"{path}.{item.name}" if path else item.name
        if _is_dataclass_type(base):
            found.extend(_walk(base, key, dotted, nested=True))
            continue
        has_default = item.default is not _MISSING or item.default_factory is not _MISSING
        found.append(
            SettingsKey(
                field=dotted,
                key=key,
                annotation=describe_annotation(base),
                required=not has_default,
                secret=typing.get_origin(base) is Secret,
                supplied_by=None,
            )
        )
    return found
