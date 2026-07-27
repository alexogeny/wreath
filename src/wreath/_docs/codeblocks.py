"""Static verification of the Python in the documentation.

Five fictions shipped in these docs in one week -- `Pool.fetchval`, `Pool.acquire`
used as an async context manager, `Database.ping`, `Depends` inside `Annotated`,
and `page_params` as a dependency. None of them exist. One of them, copied into a
service, answers `/ready` with 503 forever, because the health check catches the
`AttributeError` and reports a failed probe rather than a broken probe.

They were not five mistakes. They were the output of a corpus of 407 Python
blocks across 108 files with no mechanism at all: `docs check` validated links,
anchors and orphans, and the renderer test proved only that the renderer
survives. Nothing read the code.

This module is the floor -- the cheap half that runs over every block, including
the 269 published fragments that can never execute because they assume an `app`
the reader already has. It does two things:

**Name resolution.** An attribute chain whose root it can type is resolved
against the real object, one step at a time, and a missing attribute is an
error. `db.pool("read").fetchval(...)` resolves because `Database.pool` is
annotated `-> Pool` and `Pool` has no `fetchval`.

**A rule catalog.** Some misuse is not a missing name -- `Annotated[Session,
Depends(...)]` spells two names that both exist and means something the binder
ignores. Those are AST patterns, one per known way to hold the framework wrong,
in the same spirit as `wreath port`'s rule catalog and the `wreath audit`
ruleset. A rule earns its place by naming a mistake someone actually made.

**What it cannot do**, stated because a check whose limits are unwritten gets
trusted past them: it does not execute, so it cannot catch a wrong *argument*, a
wrong *value*, or logic that type-checks and misbehaves. It resolves a root only
when the block, an earlier block on the same page, or `VOCABULARY` gives it one,
so a chain rooted in an unknown name is *unresolved*, not *verified*. That is
why `coverage()` reports the split and a test pins it: the number of unresolved
chains may fall, never rise. Without that ratchet this would be one more check
that silently has nothing to check, which is the failure it exists to prevent.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import typing
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = [
    "VOCABULARY",
    "Block",
    "Coverage",
    "Finding",
    "check_page",
    "coverage",
    "scan",
]

_FENCE = re.compile(r"^(?P<mark>`{3,}|~{3,})(?P<info>[^\n]*)$")
_PYTHON = ("python", "py", "python3")

#: Conventional names the docs use for objects the reader is assumed to hold,
#: mapped to the type they always mean. Measured, not guessed: `app` is the root
#: of 193 attribute chains in the published corpus and is bound in a block on
#: only a handful of pages, so without this table the floor would resolve almost
#: nothing on the pages that teach the framework.
#:
#: An entry is a promise that the docs never use that name for anything else. It
#: is checked -- `_bind_block` refuses to shadow a vocabulary name with a
#: different type, so a page that means something else by `session` reports
#: rather than resolving against the wrong class.
VOCABULARY: dict[str, str] = {
    "app": "wreath:Wreath",
    "router": "wreath:Router",
    "request": "wreath:Request",
    "response": "wreath:Response",
    "db": "wreath.postgres:Database",
    "database": "wreath.postgres:Database",
    "pool": "wreath.postgres:Pool",
    "connection": "wreath.postgres:Connection",
    "conn": "wreath.postgres:Connection",
    "session": "wreath.orm:Session",
    "jobs": "wreath.jobs:JobRunner",
    "bus": "wreath.messaging:MessageBus",
    "client": "wreath.testing:TestClient",
    "http_client": "wreath.http_client:HTTPClient",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One defect in one block, addressed well enough to fix without hunting."""

    page: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.page}:{self.line}: {self.message}"


@dataclass(frozen=True, slots=True)
class Block:
    """One fenced block: its info string, body, and 1-based opening line."""

    info: str
    body: str
    line: int

    @property
    def language(self) -> str:
        return self.info.split()[0] if self.info.split() else ""

    @property
    def is_python(self) -> bool:
        return self.language in _PYTHON

    def attribute(self, name: str) -> str | None:
        """The value of a `name="..."` (or bare `name`) info-string attribute."""
        match = re.search(rf'\b{re.escape(name)}(?:="([^"]*)")?(?=\s|$)', self.info)
        if match is None:
            return None
        return match.group(1) if match.group(1) is not None else ""


@dataclass
class Coverage:
    """How much of the corpus the floor actually looked at.

    `resolved` and `unresolved` count attribute *chains*, not blocks: one block
    may hold a dozen. `unparsed` counts blocks that are not Python at all, which
    is a legitimate state -- a `python` fence around a side-by-side comparison
    or a pseudocode sketch -- but one that must be visible rather than assumed.
    """

    blocks: int = 0
    parsed: int = 0
    unparsed: int = 0
    resolved: int = 0
    unresolved: int = 0
    roots: dict[str, int] = field(default_factory=dict)

    def merge(self, other: Coverage) -> None:
        self.blocks += other.blocks
        self.parsed += other.parsed
        self.unparsed += other.unparsed
        self.resolved += other.resolved
        self.unresolved += other.unresolved
        for name, count in other.roots.items():
            self.roots[name] = self.roots.get(name, 0) + count


def scan(text: str) -> Iterator[Block]:
    """Yield each top-level fenced block in `text`.

    Nested fences are skipped by CommonMark's own rule -- a longer run opens an
    enclosing fence and everything inside it belongs to that block -- which is
    how a guide documents fence syntax without the example being scanned. The
    same rule is why `_fenced.extract` exists; this is a second reader because
    it wants every block, not one opener.
    """
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        match = _FENCE.match(lines[index])
        if match is None:
            index += 1
            continue
        mark = match.group("mark")
        info = match.group("info").strip()
        start = index
        index += 1
        body: list[str] = []
        while index < len(lines):
            closing = _FENCE.match(lines[index])
            if (
                closing is not None
                and closing.group("mark")[0] == mark[0]
                and len(closing.group("mark")) >= len(mark)
                and not closing.group("info").strip()
            ):
                break
            body.append(lines[index])
            index += 1
        index += 1
        yield Block(info, "\n".join(body), start + 1)


# --- resolving a name to a real object ---------------------------------------


def _load(spec: str) -> object | None:
    """Import `module:Qualname`, or return None if it is not there."""
    module_name, _, qualname = spec.partition(":")
    try:
        obj: object = importlib.import_module(module_name)
    except ImportError:
        return None
    for part in filter(None, qualname.split(".")):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _return_type(func: object) -> type | None:
    """The class a callable returns, when the annotation names one.

    Uses `typing.get_type_hints` so a string annotation under
    `from __future__ import annotations` -- which every wreath module uses --
    resolves against the defining module's globals. An unresolvable or
    non-class annotation returns None, which stops the chain rather than
    guessing.
    """
    if not callable(func):
        return None
    try:
        hints = typing.get_type_hints(func)
    except (NameError, TypeError, AttributeError):
        return None
    annotation = hints.get("return")
    if annotation is typing.Any or not isinstance(annotation, type):
        # `typing.Any` *is* a class in 3.11+, so an unannotated-in-effect return
        # would otherwise resolve to it and every downstream attribute would be
        # reported missing. Any means "unknown", which stops the chain.
        return None
    return annotation


class _Environment:
    """Names a page has bound, and the type each one means.

    A value is a class (an *instance* of it is what the name holds) or a module.
    The distinction matters for attribute lookup: `wreath.Wreath` is a module
    attribute, `app.get` is a lookup on the class.
    """

    def __init__(self) -> None:
        self.instances: dict[str, type] = {}
        self.objects: dict[str, object] = {}
        for name, spec in VOCABULARY.items():
            loaded = _load(spec)
            if isinstance(loaded, type):
                self.instances[name] = loaded

    def child(self) -> _Environment:
        """A copy, for bindings that must not outlive one block.

        Parameter annotations are block-local by design. A handler naming its
        argument `connection: WebSocket` says nothing about what `connection`
        means three blocks later, and persisting it would resolve a later chain
        against the wrong class.
        """
        clone = _Environment.__new__(_Environment)
        clone.instances = dict(self.instances)
        clone.objects = dict(self.objects)
        return clone

    def bind_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                try:
                    module = importlib.import_module(alias.name if alias.asname else root)
                except ImportError:
                    continue
                self.objects[alias.asname or root] = module
            return
        if node.module is None or node.level:
            return
        try:
            module = importlib.import_module(node.module)
        except ImportError:
            return
        for alias in node.names:
            member = getattr(module, alias.name, None)
            if member is not None:
                self.objects[alias.asname or alias.name] = member

    def bind_assign(self, name: str, value: ast.expr) -> None:
        """Bind `name` to the class its initialiser constructs, if evident.

        An assignment whose right-hand side cannot be typed still *rebinds* the
        name, so a vocabulary entry must be dropped rather than left standing.
        `response = await billing.get(...)` makes `response` a client response,
        not `wreath.Response`; keeping the conventional binding reported
        `response.json` as missing on a class the page never meant.
        """
        if not isinstance(value, ast.Call):
            self.bind_name(name)
            return
        target = self.resolve(value.func)
        if isinstance(target, type):
            # A vocabulary name may only be re-bound to the same type; anything
            # else means the page uses a conventional name for something else,
            # and resolving its attributes against the wrong class would invent
            # errors. Dropping it is the safe direction -- unresolved, not wrong.
            if name in VOCABULARY and self.instances.get(name) is not target:
                self.instances.pop(name, None)
                self.objects[name] = object()
                return
            self.instances[name] = target
            self.objects.pop(name, None)
        else:
            self.bind_name(name)

    def bind_name(self, name: str) -> None:
        """Record a name as bound but untyped, so it is not resolved wrongly."""
        self.instances.pop(name, None)
        self.objects[name] = object()

    def annotate(self, name: str, annotation: ast.expr) -> None:
        """Bind `name` from a written type annotation.

        Annotations beat everything else, including `VOCABULARY`: when a handler
        writes `async def feed(connection: WebSocket)`, the docs have *said* what
        the name means, and a conventional-name table that disagreed would
        invent errors. This is why the websocket guides resolve correctly
        despite `connection` conventionally meaning a database connection.
        """
        target = annotation
        # `Annotated[T, Query(...)]` -- the type is the first argument.
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            if target.value.id == "Annotated" and isinstance(target.slice, ast.Tuple):
                target = target.slice.elts[0]
        resolved = self.resolve(target)
        if isinstance(resolved, type):
            self.instances[name] = resolved
            self.objects.pop(name, None)
        else:
            self.bind_name(name)

    def resolve(self, node: ast.expr) -> object | None:
        """The object a dotted expression names, or None when unknown."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string annotation under `from __future__ import annotations`.
            try:
                return self.resolve(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return None
        if isinstance(node, ast.Name):
            if node.id in self.objects:
                return self.objects[node.id]
            return self.instances.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.resolve(node.value)
            if parent is None:
                return None
            return getattr(parent, node.attr, None)
        if isinstance(node, ast.Call):
            func = self.resolve(node.func)
            if isinstance(func, type):
                return func
            return _return_type(func)
        return None


def _root(node: ast.expr) -> ast.expr:
    """The leftmost expression of an attribute/call/subscript chain."""
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Subscript):
            current = current.value
        else:
            return current


def _dotted(node: ast.expr) -> str:
    """A readable rendering of a chain, for the error message."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_dotted(node.func)}()"
    if isinstance(node, ast.Subscript):
        return f"{_dotted(node.value)}[...]"
    return "?"


def _describe(owner: object) -> str:
    if isinstance(owner, type):
        return owner.__name__
    return getattr(owner, "__name__", repr(owner))


# --- the rule catalog --------------------------------------------------------
#
# One entry per known way to hold wreath wrong that name resolution cannot see,
# because every name in the expression exists. A rule states the mistake and the
# correct spelling, because an error that only says "no" makes the reader guess.


def _rule_depends_in_annotated(tree: ast.AST, env: _Environment) -> Iterator[str]:
    """`Annotated[T, Depends(f)]` -- the binder reads markers, not dependencies.

    Both names exist, so this is invisible to name resolution. It shipped in
    four places. The dependency is silently ignored and the parameter binds from
    the request or not at all.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if not isinstance(node.value, ast.Name) or node.value.id != "Annotated":
            continue
        if not isinstance(node.slice, ast.Tuple):
            continue
        for element in node.slice.elts[1:]:
            call = element.func if isinstance(element, ast.Call) else element
            if isinstance(call, ast.Name) and call.id == "Depends":
                yield (
                    "Depends inside Annotated is ignored by the binder -- write "
                    "it as the parameter's default: `name: T = Depends(f)`"
                )


def _rule_async_with_coroutine(tree: ast.AST, env: _Environment) -> Iterator[str]:
    """`async with f()` where `f` is a coroutine function, not an async CM.

    `Pool.acquire` is a plain coroutine; `async with pool.acquire() as conn`
    raises `TypeError: 'coroutine' object does not support the asynchronous
    context manager protocol`. It shipped in three places.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            func = env.resolve(call.func)
            if func is None or not inspect.iscoroutinefunction(func):
                continue
            returns = _return_type(func)
            if returns is not None and hasattr(returns, "__aenter__"):
                continue
            name = _dotted(call.func)
            yield (
                f"`async with {name}()` -- {name} is a coroutine function, not an "
                "async context manager; await it instead"
            )


#: The binding markers. Honoured on a *handler* parameter; inert anywhere else.
_MARKERS = ("Query(", "Path(", "Header(", "Cookie(", "Body(", "Form(", "File(")


def _rule_markers_in_a_dependency(tree: ast.AST, env: _Environment) -> Iterator[str]:
    """`Depends(f)` where `f`'s own parameters carry binding markers.

    Markers are read off a handler's signature, not a dependency's, so they do
    nothing here -- and the failure is not a quiet default. wreath calls the
    dependency with the request positionally, so the request lands in the first
    scalar parameter and the marker's own validation raises somewhere unrelated.
    Verified against a live `TestClient`: `Depends(page_params)` answers 500 with
    `TypeError: '<' not supported between instances of 'int' and 'Request'`,
    which names neither pagination nor dependencies.

    Only fires when the target resolves to a real callable, so a helper the
    block defines but never shows is not accused of a signature nobody can see.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Depends":
            continue
        if not node.args:
            continue
        target = env.resolve(node.args[0])
        if target is None or not callable(target) or isinstance(target, type):
            continue
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            continue
        marked = [
            parameter.name
            for parameter in signature.parameters.values()
            if any(marker in str(parameter.annotation) for marker in _MARKERS)
        ]
        if marked:
            yield (
                f"Depends({_dotted(node.args[0])}) -- its parameters "
                f"({', '.join(marked)}) carry binding markers, which only a "
                "handler's own signature honours; the request is passed "
                "positionally instead and the call raises"
            )


_RULES = (
    _rule_depends_in_annotated,
    _rule_async_with_coroutine,
    _rule_markers_in_a_dependency,
)


# --- the floor ---------------------------------------------------------------


def _bind_annotations(tree: ast.AST, env: _Environment) -> None:
    """Bind every written annotation in the block: parameters, then variables."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for argument in [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *(a for a in (args.vararg, args.kwarg) if a is not None),
            ]:
                if argument.annotation is not None:
                    env.annotate(argument.arg, argument.annotation)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            env.annotate(node.target.id, node.annotation)


def _bind_block(tree: ast.AST, env: _Environment) -> None:
    """Record every name the block binds, so later blocks resolve against it."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            env.bind_import(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env.bind_assign(target.id, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                env.bind_assign(node.target.id, node.value)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            env.bind_name(node.name)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            _bind_target(node.target, env)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _bind_target(node.optional_vars, env)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            env.bind_name(node.name)


def _bind_target(target: ast.expr, env: _Environment) -> None:
    """Clear any typing for a loop, `with`, or unpacking target.

    The element type of `set[WebSocket]` is not inferred, so the honest result
    is untyped. What matters is that the name stops resolving to whatever
    `VOCABULARY` says: `for connection in clients` must not be checked against
    a database connection.
    """
    if isinstance(target, ast.Name):
        env.bind_name(target.id)
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            _bind_target(element, env)


def _check_chains(
    tree: ast.AST, env: _Environment, page: str, line: int, stats: Coverage
) -> Iterator[Finding]:
    """Resolve every attribute chain, reporting the first missing step in each."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # Only the outermost Attribute of a chain is walked as a whole; inner
        # ones are reached again by ast.walk, so resolve each independently and
        # report only the step that is actually missing.
        owner = env.resolve(node.value)
        root = _root(node)
        if isinstance(root, ast.Name):
            stats.roots[root.id] = stats.roots.get(root.id, 0) + 1
        if owner is None:
            stats.unresolved += 1
            continue
        stats.resolved += 1
        if isinstance(owner, type):
            if _has_member(owner, node.attr):
                continue
            yield Finding(
                page,
                line,
                f"`{_dotted(node)}` -- {_describe(owner)} has no attribute "
                f"`{node.attr}`",
            )
        elif inspect.ismodule(owner) and not hasattr(owner, node.attr):
            yield Finding(
                page,
                line,
                f"`{_dotted(node)}` -- module {_describe(owner)} has no `{node.attr}`",
            )


def _has_member(owner: type, name: str) -> bool:
    """Whether instances of `owner` expose `name`.

    Checked against the class and its `__slots__`, because a slotted dataclass
    declares its fields there rather than as class attributes, and a false
    "no attribute" on every slotted type would make the floor unusable.
    """
    if hasattr(owner, name):
        return True
    for klass in owner.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        if name in tuple(slots):
            return True
        annotations = klass.__dict__.get("__annotations__", {})
        if name in annotations:
            return True
    return False


def check_page(text: str, page: str = "") -> tuple[list[Finding], Coverage]:
    """Check every Python block on one page. Returns (findings, coverage).

    Blocks are checked in document order against one accumulating environment,
    because that is how a reader meets them: a page that binds `app = Wreath()`
    in its first block means that `app` in its fifth.
    """
    env = _Environment()
    findings: list[Finding] = []
    stats = Coverage()
    for block in scan(text):
        if not block.is_python:
            continue
        stats.blocks += 1
        reason = block.attribute("no-check")
        try:
            tree = ast.parse(block.body)
        except SyntaxError as error:
            stats.unparsed += 1
            if reason is None:
                # A `python` fence that is not Python is either mislabelled or an
                # elided continuation of the block above. Both are fine to write
                # and neither may be silent, or the floor would skip whatever it
                # could not read -- exactly the hole it exists to close.
                findings.append(
                    Finding(
                        page,
                        block.line,
                        f"python block does not parse ({error.msg}) -- if this is a "
                        'fragment, mark the fence `no-check="why"`',
                    )
                )
            continue
        stats.parsed += 1
        # Imports first, then annotations, both into a copy: a name a handler
        # annotates is true for this block and no further.
        local = env.child()
        _bind_block(tree, local)
        _bind_annotations(tree, local)
        if reason is None:
            found = list(_check_chains(tree, local, page, block.line, stats))
            for rule in _RULES:
                found.extend(
                    Finding(page, block.line, message) for message in rule(tree, local)
                )
            findings.extend(found)
        _bind_block(tree, env)
    return findings, stats


def coverage(pages: dict[str, str]) -> Coverage:
    """Aggregate coverage across a whole corpus, ignoring findings."""
    total = Coverage()
    for page, text in pages.items():
        _, stats = check_page(text, page)
        total.merge(stats)
    return total
