"""`BaseSettings` classes, split by field shape rather than by base class."""

from __future__ import annotations

import ast

from .imports import _Imports
from .models import _model_config_value

# The four annotations `load_env`'s `dict[str, str]` converts to without a
# decision. `list`/`dict`/`Optional`/`Literal` are absent: pydantic-settings
# JSON-decodes those from the variable, and wreath hands over the raw string.
_ENV_SCALARS = frozenset({"str", "int", "float", "bool"})
_SETTINGS_FIELD_RULE = {
    "scalar": "settings.field",
    "nested": "settings.nested",
    "complex": "settings.field_complex",
}
# SettingsConfigDict keys whose effect on the ported dataclass is still fully
# determined: a literal prefix goes in front of every variable name, `extra` has
# no counterpart (reading named variables ignores the rest by construction), and
# case sensitivity only picks which spelling to look up. Anything else —
# `env_nested_delimiter`, `secrets_dir`, `env_file_encoding` on a computed path —
# changes where values come from, so the class waits for a human.
_SETTINGS_CONFIG_KEYS = frozenset(
    {
        "env_prefix",
        "extra",
        "case_sensitive",
        "env_file",
        "populate_by_name",
    }
)
# How many env names a message will spell out before it says "...".
_MAX_NAMED_ENV = 8


def settings_field_shape(
    imports: _Imports, stmt: ast.AnnAssign, settings_names: set[str] | None = None
) -> str:
    """`"nested"` | `"scalar"` | `"complex"` for one `BaseSettings` field.

    `scalar` is the shape whose whole translation is decided by the source: one
    of the four types `load_env`'s `dict[str, str]` converts to, and either no
    default (a required variable) or a literal one. Anything else — a container,
    an optional, a `Field(...)` marker, a computed default — needs someone to
    decide how the raw string becomes the value.

    `settings_names` is what separates `nested` from `complex`. A caller
    without a tree index may omit it: a sub-group then reads as `complex`, which
    keeps the *class* verdict identical, since neither shape is `scalar`.
    """
    annotation = imports.origin(stmt.annotation).split(".")[-1]
    known = settings_names or set()
    value_is_group = (
        isinstance(stmt.value, ast.Call) and imports.origin(stmt.value.func).split(".")[-1] in known
    )
    if annotation in known or value_is_group:
        return "nested"
    if annotation not in _ENV_SCALARS:
        return "complex"
    if stmt.value is None:
        return "scalar"  # no default: a required variable
    if isinstance(stmt.value, ast.Constant) and not isinstance(stmt.value.value, bytes):
        return "scalar"
    return "complex"


def settings_class_rule(
    imports: _Imports, node: ast.ClassDef, settings_names: set[str] | None = None
) -> str:
    """Whether a whole `BaseSettings` class is a field-by-field mechanical rewrite.

    Shared with the emitter, so the report and the annotation written into the
    source cannot disagree about one class. A class earns the translated verdict
    only when every field does and its configuration says nothing this analyzer
    cannot read: `env_prefix` and `extra` change the target in a way that is
    still fully determined, while an `env_nested_delimiter`, a `secrets_dir`
    or a pydantic-v1 `class Config` do not, so they hold the class back rather
    than being quietly ignored.
    """
    shapes = [
        settings_field_shape(imports, stmt, settings_names)
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.target.id != "model_config"
    ]
    if not shapes or any(shape != "scalar" for shape in shapes):
        return "settings.class"
    for stmt in node.body:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return "settings.class"  # a v1 `class Config`, or a validator
        config = _model_config_value(stmt)
        if config is None:
            continue
        if not isinstance(config, ast.Call):
            return "settings.class"
        for kw in config.keywords:
            if kw.arg not in _SETTINGS_CONFIG_KEYS or not isinstance(kw.value, ast.Constant):
                return "settings.class"
    return "settings.class_env"


def settings_required(node: ast.ClassDef) -> str:
    """The `required_env=[...]` list a settings class implies: fields with no default."""
    prefix = _env_prefix(node)
    names = [
        f"{prefix}{stmt.target.id.upper()}"
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.target.id != "model_config"
        and stmt.value is None
    ]
    note = f"every variable is read as {prefix}<FIELD>; " if prefix else ""
    if not names:
        return f"{note}every field has a default, so nothing is required at boot"
    shown = names[:_MAX_NAMED_ENV]
    listing = ", ".join(shown) + (", ..." if len(names) > len(shown) else "")
    return f"{note}required_env=[{listing}]"


def _env_prefix(node: ast.ClassDef) -> str:
    for stmt in node.body:
        config = _model_config_value(stmt)
        if isinstance(config, ast.Call):
            for kw in config.keywords:
                if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                    value = kw.value.value
                    if isinstance(value, str):
                        return value.upper()
    return ""
