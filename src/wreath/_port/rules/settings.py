"""Settings: `BaseSettings` classes and the fields wreath's `Environment` binds."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

SETTINGS: dict[str, tuple[str, str, str, str]] = {
    # Split by *field shape*, the same way `.objects.filter()` is split by
    # argument shape. A `BaseSettings` class of plain scalars with literal
    # defaults is a mechanical rewrite: pydantic-settings' default source reads
    # the field name (case-insensitively, so upper-case is the canonical
    # spelling) with `env_prefix` in front, no default means required, and
    # Environment.bind owns conversion, nested groups, aggregate errors and
    # defaults. Validators and JSON-valued settings still require judgment.
    "settings.class": (
        "settings",
        "settings",
        NEEDS_REVIEW,
        "Make this an ordinary dataclass and construct it with Environment.load('.env').bind(Settings, prefix=...). Field validators and JSON-valued settings still need an explicit conversion decision.",
    ),
    "settings.class_env": (
        "settings",
        "settings",
        TRANSLATED,
        "Make this an ordinary dataclass and bind it with Environment.load('.env').bind(Settings, prefix=...). Required fields, literal defaults and scalar conversion are automatic.",
    ),
    "settings.field": (
        "settings",
        "settings",
        TRANSLATED,
        "Environment.bind converts the annotated scalar and uses the dataclass default when the key is absent.",
    ),
    "settings.field_complex": (
        "settings",
        "settings",
        NEEDS_REVIEW,
        "Environment.bind supports optionals, unions and comma-separated containers. A JSON object encoded into one variable or a custom validator still needs an explicit adapter.",
    ),
    "settings.nested": (
        "settings",
        "settings",
        NEEDS_REVIEW,
        "Decide whether to keep the nested dataclass or flatten its access path. Environment.bind reads a kept group's fields with a double underscore, such as APP_DATABASE__HOST; a pydantic-settings JSON value for the whole group still needs an explicit adapter.",
    ),
}
