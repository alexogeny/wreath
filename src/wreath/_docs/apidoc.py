"""API-reference generation — the `:::` directive, wreath's mkdocstrings stand-in.

A reference page writes `::: wreath.response.JSONResponse` and this expands it,
before markdown rendering, into ordinary markdown: a heading, the signature in a
code fence, and the google-style docstring split into prose + Args/Returns/Raises.
Because it emits *markdown*, the anchors, slugs, and table of contents come from
the normal renderer — the reference pages travel the identical path as prose.

Introspection is stdlib `inspect` today (signatures + `getdoc`); reusing the
richer `typegen.inspect` type-resolver for cross-linked types is a follow-on.
"""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any

__all__ = ["TargetNotFound", "expand", "has_directives", "rest_markup"]

_DIRECTIVE = re.compile(r"^:::\s+([\w.]+)\s*$")
_SECTION = re.compile(
    r"^(Args|Arguments|Parameters|Returns|Raises|Yields|Example|Examples|Note|Notes):\s*$")

#: reST markup that this renderer does not speak. Docstrings reach the site as
#: markdown, and markdown has no ``double backtick`` literal and no ``:role:``
#: — so both survive into the page as damage rather than as an error.
_REST_LITERAL = re.compile(r"``[^`\n]+``")
_REST_ROLE = re.compile(r":(?:mod|class|func|meth|attr|exc|data|obj|ref|term|doc|py:\w+):`")
#: A trailing `::` opens a reST literal block. The lookbehind excludes only a
#: third colon, which is this module's own `::: target` directive -- an earlier
#: spelling excluded any word character too, which rejected `Mount it::`, the
#: single commonest form of the thing being looked for.
_REST_BLOCK = re.compile(r"(?<!:)::\s*$", re.M)
_FENCE = re.compile(r"^```.*?^```", re.M | re.S)

def has_directives(source: str) -> bool:
    return any(_DIRECTIVE.match(line) for line in source.splitlines())


def rest_markup(text: str) -> list[str]:
    """Every reST construct in *text* that the markdown renderer will mangle.

    Returns the offending snippets, so a report can quote what it found rather
    than only counting. Fenced code is stripped first: a docstring is entitled
    to show reST inside an example without the example being a defect.

    The three constructs are not cosmetic, which is why this refuses rather than
    tidies. ``double backticks`` do not nest in markdown, so a line with *two*
    of them pairs the closing backtick of the first with the opening backtick of
    the second and renders the prose between them as code -- the sentence "a
    ``pass`` entry and a ``fail`` entry" loses "entry and a" into a code span.
    A ``:role:`` prints its own name into the sentence. A trailing ``::``
    literal block prints a stray ``::`` and then renders the indented code as an
    ordinary paragraph, losing the formatting that was the point of writing it.
    All three fail silently, which is what makes them worth a gate.
    """
    prose = _FENCE.sub("", text)
    return [
        *(m.group(0) for m in _REST_LITERAL.finditer(prose)),
        *(m.group(0) for m in _REST_ROLE.finditer(prose)),
        *(m.group(0).strip() for m in _REST_BLOCK.finditer(prose)),
    ]


def expand(source: str, page: str = "", sink: list[str] | None = None) -> str:
    """Replace each `::: dotted.path` line with generated reference markdown.

    A directive naming something that cannot be imported or introspected renders
    an inline note, so a local non-strict preview still builds and shows what is
    wrong on the page itself. It *also* reports to `sink` when one is given, so
    a strict build fails rather than shipping a reference page whose body is an
    apology. `AGENTS.md` promises "a missing nav entry or broken autodoc target
    fails it"; before `sink` existed, only the first half was true.

    The catch is exactly `TargetNotFound`, which `_import` raises and nothing
    else does. A directive naming a target that is not there is the caller's
    typo; an `AttributeError` out of a *renderer* is a bug in this module, and
    turning that into an inline note on one page is how a renderer stays broken
    for a release. Both are `AttributeError` at the source, which is why
    `_import` converts one of them.

    Rendered docstrings are also checked for reST markup, which markdown does
    not speak and therefore mangles rather than rejects -- see `rest_markup`.
    That check reports through the same `sink`, so a strict build fails on it
    for the same reason it fails on an unrenderable target: the page would
    otherwise ship damaged and say nothing about it.
    """
    out: list[str] = []
    for line in source.splitlines():
        match = _DIRECTIVE.match(line)
        if match is None:
            out.append(line)
            continue
        path = match.group(1)
        try:
            rendered = _pin_anchor(_render_object(path), path)
        except TargetNotFound as error:
            out.append(f"> **API reference unavailable for `{path}`:** {error}")
            if sink is not None:
                where = f"{page}: " if page else ""
                sink.append(f"{where}::: {path} could not be rendered: {error}")
            continue
        out.append(rendered)
        if sink is not None:
            found = sorted(set(rest_markup(rendered)))
            if found:
                where = f"{page}: " if page else ""
                sink.append(
                    f"{where}::: {path} renders reST markup markdown cannot show: "
                    f"{', '.join(found[:5])}{' ...' if len(found) > 5 else ''}"
                    " -- wreath docstrings use single backticks"
                )
    return "\n".join(out)


def _pin_anchor(markdown: str, path: str) -> str:
    """Give the directive's top heading the full dotted path as its anchor id.

    mkdocstrings anchors an object at `module.Qualname`; a page cross-links to
    `#wreath.replay.FaultSchedule`. Emitting the same explicit id (via the
    renderer's `{#id}` support) keeps those references resolving under the
    native SSG instead of the auto-slug `#faultschedule`.
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#") and "{#" not in line:
            lines[index] = f"{line.rstrip()} {{#{path}}}"
            break
    return "\n".join(lines)


class TargetNotFound(Exception):
    """A `:::` directive named something that could not be resolved.

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


def _members(cls: type) -> list[tuple[str, Any, str, type | None]]:
    """Every documentable public member of *cls*, own ones first.

    Yields `(name, underlying_object, kind, inherited_from)`. `kind` is one
    of `method`, `property`, `classmethod`, `staticmethod`;
    `inherited_from` is the defining base, or `None` when the class declares
    it itself.

    Walking `vars(cls)` alone -- which is what this did -- answers a narrower
    question than the reference asks. `vars` holds only what the class body
    declared, and holds it *undescriptored*: a `property` is a `property`
    object and a `classmethod` is a `classmethod` object, neither of which
    satisfies `inspect.isfunction`. So the previous filter dropped every
    property, every classmethod, and everything a base class defined --
    `Request.method`, `Request.path` and `Request.headers` among them.

    Inheritance stops at classes wreath does not own. A model inheriting from
    `enum.Enum` or `Exception` would otherwise document `name`, `value`
    and `args` on every subclass, which is noise rather than contract.
    """
    found: list[tuple[str, Any, str, type | None]] = []
    seen: set[str] = set()
    for base in cls.__mro__:
        if base is object or not base.__module__.startswith("wreath"):
            continue
        inherited = None if base is cls else base
        for name, raw in sorted(vars(base).items()):
            if name.startswith("_") or name in seen:
                continue
            kind, obj = _classify(raw)
            if kind is None:
                continue
            seen.add(name)
            found.append((name, obj, kind, inherited))
    # Own members first: what this class declares is its contract, and what it
    # inherited is context for it.
    return [m for m in found if m[3] is None] + [m for m in found if m[3] is not None]


def _classify(raw: Any) -> tuple[str | None, Any]:
    """Map a raw `vars()` entry to `(kind, callable_or_None)`.

    Returns `(None, ...)` for anything not documentable: slot descriptors,
    dataclass field defaults, nested constants. Those already show up in the
    constructor signature or are not API.
    """
    if isinstance(raw, property):
        return "property", raw.fget
    if isinstance(raw, classmethod):
        return "classmethod", raw.__func__
    if isinstance(raw, staticmethod):
        return "staticmethod", raw.__func__
    if inspect.isfunction(raw):
        return "method", raw
    return None, None


def _render_class(cls: type, level: int) -> str:
    hashes = "#" * level
    parts = [f"{hashes} `{cls.__name__}`", ""]
    doc = inspect.getdoc(cls)
    if doc:
        parts += [_docstring(doc), ""]
    init = cls.__dict__.get("__init__")
    if init is not None and callable(init):
        parts.append(_signature_block(cls.__name__, init, skip_self=True))
    for name, member, kind, inherited in _members(cls):
        parts.append(_render_member(member, name, level + 1, kind, inherited))
    return "\n".join(parts)


#: How each member kind is labelled next to its heading. A plain method gets no
#: label -- it is the unmarked default, and labelling it would add noise to the
#: majority to distinguish the minority.
_KIND_LABEL = {
    "property": "property",
    "classmethod": "classmethod",
    "staticmethod": "staticmethod",
    "method": "",
}


def _render_member(
    func: Any, name: str, level: int, kind: str = "method", inherited: type | None = None
) -> str:
    """One member: heading, what kind it is, where it came from, then its body.

    A property renders no call signature, because it is read as an attribute;
    its return annotation carries the type instead. Everything else renders the
    signature it is called with, prefixed `async` when it must be awaited --
    a reader cannot otherwise tell, and a reference that hides it invites
    exactly the "why is this a coroutine object" confusion.
    """
    notes = [note for note in (_KIND_LABEL.get(kind, ""),) if note]
    if inherited is not None:
        notes.append(f"inherited from `{inherited.__name__}`")
    suffix = f" *({', '.join(notes)})*" if notes else ""
    parts = [f"{'#' * level} `{name}`{suffix}", ""]
    if kind == "property":
        annotation = _return_annotation(func)
        parts.append(f"```python\n{name}: {annotation}\n```" if annotation else "")
    else:
        parts.append(_signature_block(name, func, skip_self=kind != "staticmethod"))
    doc = inspect.getdoc(func)
    if doc:
        parts += ["", _docstring(doc)]
    return "\n".join(parts)


# Backwards-compatible alias: `_render_module` and `_render_object` call this for
# plain functions, where the kind and origin arguments are both defaults.
_render_callable = _render_member


def _return_annotation(func: Any) -> str:
    if func is None:
        return ""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return ""
    if sig.return_annotation is inspect.Signature.empty:
        return ""
    # A property's annotation arrives alone rather than after a `:`, so the
    # in-signature pattern below cannot see it; strip the repr quotes directly.
    return inspect.formatannotation(sig.return_annotation).strip("'")


#: `from __future__ import annotations` makes every annotation a string, so
#: `inspect.signature` renders it with `repr` and the reference fills with
#: `x: 'int'`. The quotes are an artifact of how the annotation was stored, not
#: something the reader writes, so they are removed.
_QUOTED_ANNOTATION = re.compile(r"(?<=[:>]\s)'([^']*)'")


def _unquote(text: str) -> str:
    return _QUOTED_ANNOTATION.sub(r"\1", text)


def _signature_block(name: str, func: Any, skip_self: bool) -> str:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return ""
    if skip_self:
        params = [p for p in sig.parameters.values() if p.name not in ("self", "cls")]
        sig = sig.replace(parameters=params)
    prefix = "async " if inspect.iscoroutinefunction(func) else ""
    return f"```python\n{prefix}{name}{_unquote(str(sig))}\n```"


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
