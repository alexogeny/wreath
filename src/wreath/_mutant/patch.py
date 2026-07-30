"""Making a mutation real in a live interpreter, without re-importing anything.

The obvious way to run a mutant is to rewrite the file and start a new process.
That costs a full import per mutant and, in a repository this size, turns a
thirty-second suite into an afternoon. The way taken here is to compile the
mutated module *in the parent*, lift out the one code object that changed, and
in the forked child assign it over the live function's `__code__`.

Assigning `__code__` is total: every reference to that function -- including the
ones bound by `from x import y` long before the mutation existed -- sees the new
body, because they all point at the same function object. That is what makes
`from` imports, decorators, and method lookup irrelevant here, and it is why
this module never tries to reload anything.

Two consequences shape the operators:

* The patch target is the **outermost** enclosing function, not the innermost.
  A route handler defined inside a router factory has no reachable function
  object; recompiling the factory means the *next* call to it builds a mutated
  handler, which is exactly when a test constructs its app. The cost is that a
  mutation inside a factory only lands if the test calls the factory. When it
  does not, the mutant survives for a reason that has nothing to do with the
  suite -- so `runner` verifies liveness where it can (see `CodePatch.verify`).
* A construct that runs at import time -- a module-level constant, a regex, a
  dataclass field default -- has no function to recompile. Those become
  `ValuePatch`, which rebinds the name everywhere it was imported to.
"""

from __future__ import annotations

import ast
import copy
import sys
from dataclasses import dataclass, field
from types import CodeType, FunctionType, MethodType, ModuleType
from typing import Any

_UNSET = object()

#: Values the interpreter interns or shares, for which `is` says nothing about
#: where a name came from. A rebind of one of these reaches the module that
#: declared it and no further.
_ATOMIC = (int, float, complex, str, bytes, bool, type(None), tuple, frozenset)


class PatchError(RuntimeError):
    """A mutation could not be built or applied. This tool's fault, not yours."""


def resolve_scope(module: ModuleType, scope: str) -> FunctionType:
    """Find the live function object named by a dotted `Class.method` path.

    Unwraps `staticmethod`, `classmethod`, `property` and anything that follows
    the `functools.wraps` convention, because the thing bound under the name is
    frequently not the thing that owns the code object.
    """
    current: Any = module
    for part in scope.split("."):
        current = _unwrap(current)
        try:
            current = getattr(current, part)
        except AttributeError as error:  # pragma: no cover - defensive
            raise PatchError(f"{module.__name__} has no {scope}") from error
    current = _unwrap(current)
    if not isinstance(current, FunctionType):
        raise PatchError(f"{module.__name__}.{scope} is {type(current).__name__}, not a function")
    return current


def _unwrap(obj: Any) -> Any:
    seen = 0
    while seen < 16:
        # `MethodType` first, and it is the case that actually fires. A
        # `classmethod` reached through `getattr` -- which is how `resolve_scope`
        # walks a dotted path -- has already been *invoked* as a descriptor, so
        # what arrives here is a method bound to the class and never the
        # `classmethod` object below. That made every control inside every
        # classmethod unmutatable, reported as an `error` outcome rather than as a
        # gap, which is the shape a blind spot takes when nobody counts it: 51
        # classmethods across 25 files contributed nothing to any score.
        #
        # The `staticmethod | classmethod` branch is still reachable for an object
        # read straight out of a `__dict__`, so it stays.
        if isinstance(obj, MethodType):
            obj = obj.__func__
        elif isinstance(obj, staticmethod | classmethod):
            obj = obj.__func__
        elif isinstance(obj, property):
            obj = obj.fget
        elif hasattr(obj, "__wrapped__"):
            obj = obj.__wrapped__
        else:
            return obj
        seen += 1
    return obj


def find_code(root: CodeType, qualname: str) -> CodeType | None:
    """Depth-first search for the code object compiled from one definition."""
    stack = [root]
    while stack:
        code = stack.pop()
        if code.co_qualname == qualname:
            return code
        for const in code.co_consts:
            if isinstance(const, CodeType):
                stack.append(const)
    return None


def compile_module(tree: ast.Module, filename: str) -> CodeType:
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec", dont_inherit=True, optimize=0)


def same_bytecode(left: CodeType, right: CodeType) -> bool:
    """Whether two code objects are indistinguishable to the interpreter.

    Used to answer the equivalent-mutant question *definitively* for the subset
    where it is decidable at all: if the compiler emits the same instructions
    and the same constants, the mutation changed nothing and is not a finding
    in either direction. Everything else stays honestly undecided.
    """
    if left.co_code != right.co_code:
        return False
    if left.co_names != right.co_names or left.co_varnames != right.co_varnames:
        return False
    if len(left.co_consts) != len(right.co_consts):
        return False
    for a, b in zip(left.co_consts, right.co_consts, strict=True):
        if isinstance(a, CodeType) or isinstance(b, CodeType):
            if not (isinstance(a, CodeType) and isinstance(b, CodeType) and same_bytecode(a, b)):
                return False
        elif type(a) is not type(b) or a != b:
            return False
    return True


@dataclass
class CodePatch:
    """Replace one function's body with the mutated compilation of it."""

    module_name: str
    scope: str
    code: CodeType
    _previous: CodeType | None = field(default=None, repr=False)

    def target(self) -> FunctionType:
        module = sys.modules.get(self.module_name)
        if module is None:  # pragma: no cover - the runner imports first
            raise PatchError(f"{self.module_name} is not imported")
        return resolve_scope(module, self.scope)

    def verify(self) -> None:
        """Refuse to build a patch the interpreter would reject.

        `__code__` assignment requires matching argument and free-variable
        counts. A mismatch means the recompiled definition sits in a different
        closure than the live one -- which happens when a module has been
        rewritten since import. Better a loud ERROR than a mutant that quietly
        never applied and was scored as survived.
        """
        live = self.target().__code__
        if live.co_freevars != self.code.co_freevars:
            raise PatchError(
                f"{self.scope} closes over {live.co_freevars} live but "
                f"{self.code.co_freevars} as compiled"
            )
        if live.co_argcount != self.code.co_argcount:
            raise PatchError(f"{self.scope} argument count moved since import")

    def is_noop(self) -> bool:
        return same_bytecode(self.target().__code__, self.code)

    def apply(self) -> None:
        function = self.target()
        self._previous = function.__code__
        function.__code__ = self.code

    def undo(self) -> None:
        if self._previous is not None:
            self.target().__code__ = self._previous
            self._previous = None


@dataclass
class ValuePatch:
    """Rebind a module-level or class-level name, everywhere it was imported to.

    A `from wreath.crud import SENSITIVE_FIELD` in another module copies the
    object into that module's globals; setting the attribute on the defining
    module alone would leave the copy enforcing the original control. So this
    sweeps `sys.modules` for every global that *is* the old object and rebinds
    each one -- but only where identity is a real question. See `_find_aliases`:
    `20` is one object shared by every module that mentions it, and rebinding
    all of them is a different tool than this one.
    """

    module_name: str
    path: tuple[str, ...]
    value: Any
    _previous: Any = field(default=_UNSET, repr=False)
    _aliases: tuple[tuple[ModuleType, str], ...] = field(default=(), repr=False)

    def _container(self) -> Any:
        module = sys.modules.get(self.module_name)
        if module is None:  # pragma: no cover - the runner imports first
            raise PatchError(f"{self.module_name} is not imported")
        current: Any = module
        for part in self.path[:-1]:
            current = getattr(current, part)
        return current

    def current(self) -> Any:
        return getattr(self._container(), self.path[-1], _UNSET)

    def is_noop(self) -> bool:
        old = self.current()
        try:
            return bool(old == self.value)
        except (TypeError, ValueError):
            return False

    def apply(self) -> None:
        container = self._container()
        name = self.path[-1]
        self._previous = getattr(container, name, _UNSET)
        self._aliases = self._find_aliases(container)
        setattr(container, name, self.value)
        for module, key in self._aliases:
            setattr(module, key, self.value)

    def _find_aliases(self, container: Any) -> tuple[tuple[ModuleType, str], ...]:
        """Where else this exact object is bound -- when that question is real.

        For an interned value it is not. `_DEFAULT_PAGE_SIZE = 20` and
        `ssl.X509_V_ERR_UNABLE_TO_GET_CRL = 20` are the *same object*, so an
        identity sweep for `20` proposes rewriting an unrelated constant in the
        standard library, and the first thing it hit was a read-only one that
        raised. It would have been far worse if it had succeeded: a mutant whose
        real effect is somewhere nobody was looking is not a finding, it is
        noise wearing a finding's clothes.

        So the sweep runs only where identity means what it looks like it
        means. For an atomic value the patch reaches the defining module alone,
        and a `from x import LIMIT` elsewhere keeps the original -- a real
        limitation, and the honest one.
        """
        if isinstance(self._previous, _ATOMIC) or self._previous is _UNSET:
            return ()
        if len(self.path) != 1:
            return ()
        aliases: list[tuple[ModuleType, str]] = []
        for module in list(sys.modules.values()):
            namespace = getattr(module, "__dict__", None)
            if namespace is None or module is container:
                continue
            for key, bound in list(namespace.items()):
                if bound is self._previous:
                    aliases.append((module, key))
        return tuple(aliases)

    def undo(self) -> None:
        if self._previous is _UNSET:
            return
        setattr(self._container(), self.path[-1], self._previous)
        for module, key in self._aliases:
            setattr(module, key, self._previous)
        self._previous = _UNSET
        self._aliases = ()


@dataclass
class AttributePatch:
    """Delete or overwrite an attribute a decorator wrote onto a live object.

    Wreath's authorization decorators do not wrap; they stamp
    `__wreath_auth_requirement__` onto the endpoint. Removing that attribute is
    the exact runtime equivalent of deleting the decorator line, and it works
    on an application's handlers as readily as on this repository's.
    """

    owner: Any
    attribute: str
    value: Any = _UNSET
    _previous: Any = field(default=_UNSET, repr=False)

    def is_noop(self) -> bool:
        return getattr(self.owner, self.attribute, _UNSET) is _UNSET and self.value is _UNSET

    def apply(self) -> None:
        self._previous = getattr(self.owner, self.attribute, _UNSET)
        if self.value is _UNSET:
            if self._previous is not _UNSET:
                delattr(self.owner, self.attribute)
        else:
            setattr(self.owner, self.attribute, self.value)

    def undo(self) -> None:
        if self._previous is _UNSET:
            if self.value is not _UNSET:
                try:
                    delattr(self.owner, self.attribute)
                except AttributeError:
                    pass
            return
        setattr(self.owner, self.attribute, self._previous)
        self._previous = _UNSET


def transform_module(tree: ast.Module, node_id: int, mutate: Any) -> ast.Module:
    """Deep-copy `tree` and apply `mutate` to the node tagged `node_id`.

    Nodes are tagged once, before any copying, and `copy.deepcopy` carries the
    tag along -- so the target is found by identity of intent rather than by
    replaying a traversal order that a transform may have changed.
    """
    clone = copy.deepcopy(tree)
    applier = _Applier(node_id, mutate)
    result = applier.visit(clone)
    if not applier.hit:
        raise PatchError(f"node {node_id} vanished before it could be mutated")
    return result


class _Applier(ast.NodeTransformer):
    def __init__(self, node_id: int, mutate: Any) -> None:
        self.node_id = node_id
        self.mutate = mutate
        self.hit = False

    def visit(self, node: ast.AST) -> Any:
        self.generic_visit(node)
        if getattr(node, "_mutant_id", None) == self.node_id:
            self.hit = True
            return self.mutate(node)
        return node
