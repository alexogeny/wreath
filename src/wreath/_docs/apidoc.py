"""API-reference generation — the ``:::`` directive, wreath's mkdocstrings stand-in.

A reference page writes ``::: wreath.response.JSONResponse`` and this expands it,
before markdown rendering, into ordinary markdown: a heading, the signature in a
code fence, and the google-style docstring split into prose + Args/Returns/Raises.
Because it emits *markdown*, the anchors, slugs, and table of contents come from
the normal renderer — the reference pages travel the identical path as prose.

Introspection is stdlib ``inspect`` today (signatures + ``getdoc``); reusing the
richer ``typegen.inspect`` type-resolver for cross-linked types is a follow-on.
"""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any

__all__ = ["TargetNotFound", "expand", "has_directives"]

_DIRECTIVE = re.compile(r"^:::\s+([\w.]+)\s*$")
_SECTION = re.compile(
    r"^(Args|Arguments|Parameters|Returns|Raises|Yields|Example|Examples|Note|Notes):\s*$")


def has_directives(source: str) -> bool:
    return any(_DIRECTIVE.match(line) for line in source.splitlines())


def expand(source: str, page: str = "", sink: list[str] | None = None) -> str:
    """Replace each ``::: dotted.path`` line with generated reference markdown.

    A directive naming something that cannot be imported or introspected renders
    an inline note, so a local non-strict preview still builds and shows what is
    wrong on the page itself. It *also* reports to ``sink`` when one is given, so
    a strict build fails rather than shipping a reference page whose body is an
    apology. `AGENTS.md` promises "a missing nav entry or broken autodoc target
    fails it"; before ``sink`` existed, only the first half was true.

    The catch is exactly `TargetNotFound`, which `_import` raises and nothing
    else does. A directive naming a target that is not there is the caller's
    typo; an `AttributeError` out of a *renderer* is a bug in this module, and
    turning that into an inline note on one page is how a renderer stays broken
    for a release. Both are `AttributeError` at the source, which is why
    `_import` converts one of them.
    """
    out: list[str] = []
    for line in source.splitlines():
        match = _DIRECTIVE.match(line)
        if match is None:
            out.append(line)
            continue
        path = match.group(1)
        try:
            out.append(_pin_anchor(_render_object(path), path))
        except TargetNotFound as error:
            out.append(f"> **API reference unavailable for `{path}`:** {error}")
            if sink is not None:
                where = f"{page}: " if page else ""
                sink.append(f"{where}::: {path} could not be rendered: {error}")
    return "\n".join(out)


def _pin_anchor(markdown: str, path: str) -> str:
    """Give the directive's top heading the full dotted path as its anchor id.

    mkdocstrings anchors an object at ``module.Qualname``; a page cross-links to
    ``#wreath.replay.FaultSchedule``. Emitting the same explicit id (via the
    renderer's ``{#id}`` support) keeps those references resolving under the
    native SSG instead of the auto-slug ``#faultschedule``.
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#") and "{#" not in line:
            lines[index] = f"{line.rstrip()} {{#{path}}}"
            break
    return "\n".join(lines)


class TargetNotFound(Exception):
    """A ``:::`` directive named something that could not be resolved.

    Distinct from the `AttributeError` `getattr` would raise, so `expand` can
    catch a caller's typo without also catching a renderer's bug -- the two are
    the same exception type and only this boundary knows which is which.
    """


def _import(path: str) -> Any:
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError:
        module_path, _, attr = path.rpartition(".")
        if not module_path:
            raise TargetNotFound(f"{path!r} is not a module") from None
        try:
            module = importlib.import_module(module_path)
        except ImportError as error:
            raise TargetNotFound(f"cannot import {module_path!r}: {error}") from None
        try:
            return getattr(module, attr)
        except AttributeError:
            raise TargetNotFound(f"{module_path!r} has no attribute {attr!r}") from None


def _render_object(path: str) -> str:
    obj = _import(path)
    if inspect.ismodule(obj):
        return _render_module(obj, path)
    if inspect.isclass(obj):
        return _render_class(obj, level=3)
    if callable(obj):
        return _render_callable(obj, path.rpartition(".")[2], level=3)
    return f"### `{path}`\n\n{_docstring(inspect.getdoc(obj))}"


def _public_names(module: object) -> list[str]:
    names = getattr(module, "__all__", None)
    if names is not None:
        return list(names)
    return [n for n in dir(module) if not n.startswith("_")]


def _render_module(module: Any, path: str) -> str:
    parts = [f"## `{path}`", "", _docstring(inspect.getdoc(module)), ""]
    own = module.__name__
    for name in _public_names(module):
        member = getattr(module, name, None)
        if inspect.isclass(member) and member.__module__ == own:
            parts.append(_render_class(member, level=3))
        elif inspect.isfunction(member) and getattr(member, "__module__", None) == own:
            parts.append(_render_callable(member, name, level=3))
    return "\n".join(parts)


def _render_class(cls: type, level: int) -> str:
    hashes = "#" * level
    parts = [f"{hashes} `{cls.__name__}`", ""]
    doc = inspect.getdoc(cls)
    if doc:
        parts += [_docstring(doc), ""]
    init = cls.__dict__.get("__init__")
    if init is not None and callable(init):
        parts.append(_signature_block(cls.__name__, init, skip_self=True))
    for name, member in sorted(vars(cls).items()):
        if name.startswith("_") or not inspect.isfunction(member):
            continue
        parts.append(_render_callable(member, name, level=level + 1, skip_self=True))
    return "\n".join(parts)


def _render_callable(func: Any, name: str, level: int, skip_self: bool = False) -> str:
    hashes = "#" * level
    parts = [f"{hashes} `{name}`", "", _signature_block(name, func, skip_self)]
    doc = inspect.getdoc(func)
    if doc:
        parts += ["", _docstring(doc)]
    return "\n".join(parts)


def _signature_block(name: str, func: Any, skip_self: bool) -> str:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return ""
    if skip_self:
        params = [p for p in sig.parameters.values() if p.name != "self"]
        sig = sig.replace(parameters=params)
    return f"```python\n{name}{sig}\n```"


def _docstring(doc: str | None) -> str:
    """Format a google-style docstring: prose kept, sections turned into lists."""
    if not doc:
        return ""
    lines = doc.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        section = _SECTION.match(lines[i].strip())
        if section is None:
            out.append(lines[i])
            i += 1
            continue
        out += ["", f"**{section.group(1)}:**", ""]
        i += 1
        while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            item = lines[i].strip()
            if item:
                name, sep, rest = item.partition(":")
                out.append(f"- `{name.strip()}` — {rest.strip()}" if sep else f"- {item}")
            i += 1
    return "\n".join(out).strip()
