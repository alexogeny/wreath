"""The operator library: each one names a control and takes it away.

A general mutation tester flips `<` to `<=` and deletes statements at random,
because it knows nothing about the program. It therefore spends most of its
budget on mutants nobody would ever ship, and reports a score dominated by
arithmetic nobody was worried about.

These operators are scoped the other way. Every one of them corresponds to a
sentence somebody could say in a post-mortem: *the role check was dropped*, *the
refusal never fired*, *the withheld column became writable*, *the rate limit was
keyed on something the caller mints for free*. That scoping is what makes the
equivalent-mutant problem tractable here -- deleting a `raise` that refuses a
request is almost never semantically equivalent -- and it is why a surviving
mutant reads as a question rather than as noise.

Two families:

**Declaration operators** work on the construction of a declared object -- an
`AuthRequirement(...)`, a `Tool(...)`, a `crud_router(...)`, a rate limit, a
Cedar policy string. They only ever drop a keyword the callee has a default
for, which is checked against the *live* signature rather than guessed, so a
dropped control is always a call the program could really have made.

**Predicate operators** work on the enforcement of a control -- a clause in an
authorization conjunction, a guard that refuses, an ownership test. These are
confined to functions whose own text names a control (see `CONTROL_TOKENS`),
which is a heuristic and is documented as one: it is why `wreath mutant` does
not offer to mutate the JSON encoder.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

#: A function is a candidate for the predicate operators when its own source
#: names one of these. The list is deliberately readable rather than clever: it
#: is the vocabulary of access control, secrecy, and bounds. Widening it costs
#: run time; narrowing it costs findings. It is printed by `--explain`.
CONTROL_TOKENS: frozenset[str] = frozenset({
    "admin", "allow", "audience", "authent", "authoriz", "backup", "bound",
    "capab", "ceiling", "cert", "challenge", "claim", "cookie", "cors",
    "credential", "csrf", "decrypt", "deny", "digest", "encrypt", "escape",
    "expire", "exposure", "forbid", "grant", "guard", "hash", "hmac",
    "identif", "identity", "issuer", "leeway", "limit", "mfa", "nonce",
    "origin", "owner", "owns", "passcode", "password", "pending", "permiss",
    "permit", "policy", "policies", "principal", "privileg", "quota",
    "readonly", "redact", "refus", "reject", "replay", "requirement",
    "revoke", "role", "rotate", "sandbox", "sanitiz", "scope", "secret",
    "secure", "sensitive", "session", "signature", "skew", "sortable",
    "stamp", "tenant", "throttle", "token", "totp", "traversal", "trust",
    "unauthor", "verif", "webauthn", "withheld", "writable",
})

#: Keywords that *are* a control when they appear at a call site. Dropping one
#: is the source-level spelling of "this control was never declared".
CONTROL_KEYWORDS: frozenset[str] = frozenset({
    "action", "algorithms", "allow", "allow_list", "allowed", "audience",
    "authenticated", "burst", "challenge", "cost", "csrf", "dependencies",
    "elicitation", "exempt", "expose", "http_only", "identify", "issuer",
    "key", "limit", "limits", "max_age", "middleware", "origins", "policies",
    "policy", "permissions", "rate_limit", "readonly", "require_user_verification",
    "requirement", "resource", "roles", "rp_id", "same_site", "sampling",
    "scopes", "second_factor", "secure", "sensitive", "skew", "sortable_fields",
    "verifier", "window",
})

#: Keywords and constant names that are numeric ceilings. Widening one to
#: `_WIDE` is the "limit that does not limit" mutation.
LIMIT_TOKENS: frozenset[str] = frozenset({
    "burst", "capacity", "ceiling", "chunk", "cost", "deadline", "depth",
    "idle", "leeway", "limit", "max", "period", "quota", "rate", "seconds",
    "size", "skew", "timeout", "ttl", "window",
})

#: What a widened bound becomes. Large enough that nothing reaches it, small
#: enough that arithmetic on it does not overflow a C `int` in the native paths.
_WIDE = 1 << 40

#: Names whose value is a deny-list: emptying one removes every refusal in it.
DENY_TOKENS: frozenset[str] = frozenset({
    "banned", "blocked", "denied", "deny", "deferred", "disallowed",
    "forbidden", "refused", "reserved", "unsafe",
})

#: Names whose value is a redaction pattern: a pattern that matches nothing
#: redacts nothing.
REDACTION_TOKENS: frozenset[str] = frozenset({
    "sensitive", "secret", "redact", "private", "scrub", "mask",
})

#: A regex that can never match anything, for `value.disable-pattern`.
_NEVER = re.compile(r"(?!x)x")

#: Function names that read as "may this caller do this?". Their bodies become
#: `return True`.
PREDICATE_TOKENS: frozenset[str] = frozenset({
    "allow", "authoriz", "belongs", "can_", "check", "eligible", "entitled",
    "has_", "is_", "may_", "owns", "owned", "permit", "valid", "verif",
})

#: Call targets whose result establishes who the caller is. Deleting the call
#: is how a control ends up keyed on something the caller mints for free.
ESTABLISH_TOKENS: frozenset[str] = frozenset({
    "authenticate", "authorize", "check", "identify", "refresh", "resolve",
    "rotate", "touch", "validate", "verify",
})

_IGNORED_IF_TESTS = ("TYPE_CHECKING", "__name__")


@dataclass
class Candidate:
    """One mutation, before it has been compiled or resolved."""

    operator: str
    control: str
    line: int
    scope: tuple[str, ...]
    """Enclosing definitions, outermost function last. Empty at module level."""

    node_id: int = -1
    watch: tuple[int, ...] = ()
    """Extra lines whose execution implies this construct was evaluated.

    A clause halfway through a multi-line `and` gets its own line number, and
    short-circuiting means it may never be reached even when the statement runs.
    Watching the enclosing statement as well keeps the candidate set a superset:
    over-selecting costs run time, under-selecting invents a survivor.
    """

    mutate: Callable[[ast.AST], ast.AST] | None = None
    value_path: tuple[str, ...] = ()
    value: Any = None
    kind: str = "code"

    @property
    def scope_name(self) -> str:
        return ".".join(self.scope)


@dataclass
class _Context:
    module: ModuleType | None
    tree: ast.Module
    scopes: dict[int, tuple[str, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# tagging


def tag(tree: ast.Module) -> dict[int, tuple[str, ...]]:
    """Give every node an id and the scope whose code object owns it.

    The scope recorded is the *outermost* enclosing function, because that is
    the one with a live function object to patch. Classes contribute their name
    to the path until a function is entered; after that the path is frozen.

    A decorator is the exception, and it is not a subtle one: `@app.get("/x",
    dependencies=...)` is *evaluated where the `def` is written*, not inside the
    function it decorates, so it belongs to the enclosing scope. Attributing it
    to the function instead produced a mutation that recompiled a body nobody
    had changed -- caught only because the bytecode came out identical, which is
    luck rather than a design.
    """
    scopes: dict[int, tuple[str, ...]] = {}
    counter = itertools.count()

    def walk(
        node: ast.AST,
        stack: tuple[str, ...],
        inside: bool,
        outer: tuple[tuple[str, ...], bool],
    ) -> None:
        node_id = next(counter)
        setattr(node, "_mutant_id", node_id)  # noqa: B010 - AST nodes have no such slot
        scopes[node_id] = stack
        decorators = getattr(node, "decorator_list", ())
        here = (stack, inside)
        for child in ast.iter_child_nodes(node):
            if any(child is decorator for decorator in decorators):
                walk(child, outer[0], outer[1], outer)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                walk(child, stack if inside else (*stack, child.name), True, here)
            elif isinstance(child, ast.ClassDef):
                walk(child, stack if inside else (*stack, child.name), inside, here)
            else:
                walk(child, stack, inside, outer)

    walk(tree, (), False, ((), False))
    return scopes


# ---------------------------------------------------------------------------
# helpers


def _names_in(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id.lower())
        elif isinstance(child, ast.Attribute):
            found.add(child.attr.lower())
        elif isinstance(child, ast.arg):
            found.add(child.arg.lower())
        elif isinstance(child, ast.keyword) and child.arg:
            found.add(child.arg.lower())
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value.lower()[:120])
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(child.name.lower())
    return found


def _matches(names: set[str], tokens: frozenset[str]) -> bool:
    return any(token in name for name in names for token in tokens)


def _short(node: ast.AST, width: int = 72) -> str:
    try:
        text = ast.unparse(node)
    except (AttributeError, ValueError, TypeError):  # pragma: no cover
        return "<unprintable>"
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _resolve_callee(module: ModuleType | None, node: ast.expr) -> Any:
    """Best-effort lookup of what a call site actually calls.

    Used only to ask whether a keyword has a default. When the answer is not
    knowable the operator declines to produce a mutation, because a call that
    raises `TypeError` is a broken mutant, and a broken mutant that kills a test
    inflates the score with something the suite did not actually catch.
    """
    if module is None:
        return None
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    obj: Any = module
    for part in reversed(parts):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _route_metadata() -> frozenset[str]:
    """Route keywords that reach `RouteDefinition` through `**metadata`.

    `@app.get(path, dependencies=..., middleware=...)` is spelled with a
    `**metadata` catch-all, so asking the decorator's signature whether
    `dependencies` has a default answers "there is no such parameter" and the
    operator would decline -- silently leaving *the* control this tool was built
    to remove unmutated. The answer lives one layer down, on the record the
    metadata is forwarded to.
    """
    try:
        from dataclasses import MISSING, fields

        from ..router import RouteDefinition
    except ImportError:  # pragma: no cover - wreath is always importable here
        return frozenset()
    return frozenset(
        field.name
        for field in fields(RouteDefinition)
        if field.default is not MISSING or field.default_factory is not MISSING
    )


#: Attributes that introduce a route when called with a literal path.
_ROUTE_VERBS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "route", "websocket"}
)


def _looks_like_a_route(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in _ROUTE_VERBS
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("/")
    )


def _defaulted_keywords(callee: Any) -> frozenset[str] | None:
    try:
        signature = inspect.signature(callee)
    except (TypeError, ValueError):
        return None
    names: set[str] = set()
    variadic = False
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            variadic = True
            continue
        if parameter.default is not inspect.Parameter.empty:
            names.add(name)
    if variadic:
        # A `**kwargs` callee cannot be asked in general -- dropping a keyword it
        # forwards somewhere required would be a broken mutant, and a broken
        # mutant that kills a test inflates the score. The one case that *is*
        # knowable is a route decorator, so that one is answered and the rest
        # decline.
        names &= _route_metadata()
        if not names:
            return None
    return frozenset(names)


# ---------------------------------------------------------------------------
# transforms (each returns a *new* node for the tagged one)


def _drop_boolop_operand(index: int) -> Callable[[ast.AST], ast.AST]:
    def mutate(node: ast.AST) -> ast.AST:
        if not isinstance(node, ast.BoolOp):  # pragma: no cover - defensive
            raise TypeError(f"drop-operand wants a BoolOp, got {type(node).__name__}")
        values = [v for i, v in enumerate(node.values) if i != index]
        return values[0] if len(values) == 1 else ast.BoolOp(op=node.op, values=values)

    return mutate


def _want[T: ast.AST](node: ast.AST, kind: type[T]) -> T:
    """Narrow, loudly. A transform applied to the wrong node is a defect in
    this tool, and it must not be mistaken for a mutation the suite missed."""
    if not isinstance(node, kind):
        raise TypeError(f"expected {kind.__name__}, got {type(node).__name__}")
    return node


def _drop_keyword(index: int) -> Callable[[ast.AST], ast.AST]:
    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        call.keywords = [k for i, k in enumerate(call.keywords) if i != index]
        return call

    return mutate


def _set_keyword(index: int, value: ast.expr) -> Callable[[ast.AST], ast.AST]:
    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        call.keywords[index].value = value
        return call

    return mutate


def _to_pass(_node: ast.AST) -> ast.AST:
    return ast.Pass()


def _drop_comprehension_if(index: int) -> Callable[[ast.AST], ast.AST]:
    def mutate(node: ast.AST) -> ast.AST:
        clause = _want(node, ast.comprehension)
        clause.ifs = [c for i, c in enumerate(clause.ifs) if i != index]
        return clause

    return mutate


def _take_branch(body: bool) -> Callable[[ast.AST], ast.AST]:
    def mutate(node: ast.AST) -> ast.AST:
        choice = _want(node, ast.IfExp)
        return choice.body if body else choice.orelse

    return mutate


def _replace_test(value: bool) -> Callable[[ast.AST], ast.AST]:
    def mutate(node: ast.AST) -> ast.AST:
        branch = _want(node, ast.If)
        branch.test = ast.Constant(value=value)
        return branch

    return mutate


def _return_true(node: ast.AST) -> ast.AST:
    function = node if isinstance(node, ast.AsyncFunctionDef) else _want(node, ast.FunctionDef)
    function.body = [ast.Return(value=ast.Constant(value=True))]
    return function


def _rewrite_string(new: str) -> Callable[[ast.AST], ast.AST]:
    def mutate(_node: ast.AST) -> ast.AST:
        return ast.Constant(value=new)

    return mutate


# ---------------------------------------------------------------------------
# the operators


def _control_functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = _names_in(node)
            names.add(node.name.lower())
            if _matches(names, CONTROL_TOKENS):
                yield node


def _predicate_operators(context: _Context) -> Iterator[Candidate]:
    """Weaken enforcement inside functions whose own text names a control."""
    seen: set[int] = set()
    for function in _control_functions(context.tree):
        for node in ast.walk(function):
            node_id = getattr(node, "_mutant_id", -1)
            if node_id in seen:
                continue
            seen.add(node_id)
            scope = context.scopes.get(node_id, ())
            if not scope:
                continue

            if isinstance(node, ast.BoolOp) and len(node.values) > 1:
                joiner = "and" if isinstance(node.op, ast.And) else "or"
                for index, operand in enumerate(node.values):
                    yield Candidate(
                        operator="predicate.drop-operand",
                        control=f"clause `{_short(operand, 56)}` in a compound "
                                f"{joiner} condition",
                        line=getattr(operand, "lineno", node.lineno),
                        scope=scope,
                        node_id=node_id,
                        watch=(node.lineno, node.values[0].lineno),
                        mutate=_drop_boolop_operand(index),
                    )

            elif isinstance(node, ast.comprehension) and node.ifs:
                # A withheld-field set is almost always one comprehension with
                # one filter: `{k for k in columns if k not in sensitive}`.
                # There is no boolean operator to weaken, so the filter itself
                # is the control, and dropping it is how a redaction stops
                # redacting.
                for index, clause in enumerate(node.ifs):
                    yield Candidate(
                        operator="comprehension.drop-clause",
                        control=f"the filter `if {_short(clause, 56)}` on a comprehension",
                        line=getattr(clause, "lineno", node.target.lineno),
                        scope=scope,
                        node_id=node_id,
                        watch=(node.target.lineno, node.iter.lineno),
                        mutate=_drop_comprehension_if(index),
                    )

            elif isinstance(node, ast.IfExp):
                # `key = principal if principal is not None else session.id` is
                # the shape of "keyed on the caller, or on something they mint
                # for free". Collapsing it to either branch is the cheapest
                # possible statement of that bug.
                for keep_body, branch in ((True, node.body), (False, node.orelse)):
                    yield Candidate(
                        operator="expression.take-branch",
                        control=f"the choice in `{_short(node, 56)}` "
                                f"(always `{_short(branch, 32)}`)",
                        line=node.lineno,
                        scope=scope,
                        node_id=node_id,
                        watch=(node.lineno, node.body.lineno, node.orelse.lineno),
                        mutate=_take_branch(keep_body),
                    )

            elif isinstance(node, ast.Raise) and node.exc is not None:
                yield Candidate(
                    operator="guard.remove-raise",
                    control=f"the refusal `raise {_short(node.exc, 56)}`",
                    line=node.lineno,
                    scope=scope,
                    node_id=node_id,
                    mutate=_to_pass,
                )

            elif isinstance(node, ast.If):
                if isinstance(node.test, ast.Name) and node.test.id in _IGNORED_IF_TESTS:
                    continue
                if isinstance(node.test, ast.Compare) and _short(node.test).startswith("__name__"):
                    continue
                yield Candidate(
                    operator="guard.never-fires",
                    control=f"the guarded branch `if {_short(node.test, 56)}`",
                    line=node.lineno,
                    scope=scope,
                    node_id=node_id,
                    mutate=_replace_test(False),
                )
                if not node.orelse:
                    yield Candidate(
                        operator="guard.always-fires",
                        control=f"the condition on `if {_short(node.test, 56)}`",
                        line=node.lineno,
                        scope=scope,
                        node_id=node_id,
                        mutate=_replace_test(True),
                    )

            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Await | ast.Call):
                call = node.value.value if isinstance(node.value, ast.Await) else node.value
                if not isinstance(call, ast.Call):
                    continue
                target = call.func
                name = target.attr if isinstance(target, ast.Attribute) else getattr(
                    target, "id", "")
                if not any(token in name.lower() for token in ESTABLISH_TOKENS):
                    continue
                yield Candidate(
                    operator="guard.drop-statement",
                    control=f"the call `{_short(node.value, 56)}` that establishes "
                            f"what a later check reads",
                    line=node.lineno,
                    scope=scope,
                    node_id=node_id,
                    mutate=_to_pass,
                )

        name = function.name.lower()
        if any(token in name for token in PREDICATE_TOKENS):
            returns = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
            if returns and len(function.body) > 1:
                node_id = getattr(function, "_mutant_id", -1)
                scope = context.scopes.get(node_id, ())
                if scope:
                    # The `def` line runs once, at import. Watching it would
                    # attribute the mutation to no test at all and report a
                    # covered control as unreached, so the body is what counts.
                    yield Candidate(
                        operator="predicate.always-true",
                        control=f"every check in `{function.name}` (it now answers True)",
                        line=function.lineno,
                        scope=scope,
                        node_id=node_id,
                        watch=tuple(
                            child.lineno
                            for child in ast.walk(function)
                            if isinstance(child, ast.stmt)
                        ),
                        mutate=_return_true,
                    )


def _declaration_operators(context: _Context) -> Iterator[Candidate]:
    """Undeclare a control at the call site that declares it."""
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.Call) or not node.keywords:
            continue
        node_id = getattr(node, "_mutant_id", -1)
        scope = context.scopes.get(node_id, ())
        if not scope:
            continue
        callee = _resolve_callee(context.module, node.func)
        defaults = _defaulted_keywords(callee) if callee is not None else None
        if defaults is None and _looks_like_a_route(node):
            # `@app.get("/x", dependencies=...)` inside a factory: `app` is a
            # local, so the callee cannot be resolved and the route's own
            # controls would go unmutated. The verb, the literal path and the
            # keyword together are specific enough to answer without it.
            defaults = _route_metadata()
        label = _short(node.func, 40)
        for index, keyword in enumerate(node.keywords):
            if keyword.arg is None:
                continue
            name = keyword.arg
            lowered = name.lower()
            if name in CONTROL_KEYWORDS and defaults is not None and name in defaults:
                yield Candidate(
                    operator="declaration.drop-keyword",
                    control=f"`{name}=` on `{label}(...)` (it falls back to the default)",
                    line=keyword.value.lineno,
                    scope=scope,
                    node_id=node_id,
                    watch=(node.lineno, node.func.lineno),
                    mutate=_drop_keyword(index),
                )
            if (
                any(token in lowered for token in LIMIT_TOKENS)
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, int | float)
                and not isinstance(keyword.value.value, bool)
            ):
                yield Candidate(
                    operator="declaration.widen-bound",
                    control=f"the bound `{name}={keyword.value.value}` on "
                            f"`{label}(...)` (widened past reach)",
                    line=keyword.value.lineno,
                    scope=scope,
                    node_id=node_id,
                    watch=(node.lineno, node.func.lineno),
                    mutate=_set_keyword(index, ast.Constant(value=_WIDE)),
                )


_CEDAR_FORBID = re.compile(r"\bforbid\s*\(")
_CEDAR_CONDITION = re.compile(r"\b(when|unless)\s*\{[^{}]*\}", re.DOTALL)


def _cedar_offer(
    line: int, scope: tuple[str, ...], node_id: int, name: str | None
) -> Callable[[str, str, str], Candidate]:
    """A policy inside a function is recompiled; one at module level is rebound."""

    def offer(operator: str, control: str, replacement: str) -> Candidate:
        if scope:
            return Candidate(
                operator=operator, control=control, line=line, scope=scope,
                node_id=node_id, mutate=_rewrite_string(replacement),
            )
        return Candidate(
            operator=operator, control=control, line=line, scope=(),
            kind="value", value_path=(name or "",), value=replacement,
        )

    return offer


def _cedar_operators(context: _Context) -> Iterator[Candidate]:
    """Mutate Cedar policy text wherever it is written down.

    Cedar policies are a declared object with a textual surface, so they get
    operators of their own: flip an effect, delete a policy, drop a condition.
    Any string literal that parses as Cedar counts.

    Most applications write theirs as a module-level constant, which has no
    enclosing function to recompile -- so those become value patches, rebinding
    the name in the defining module and everywhere it was imported to. A policy
    loaded from a `.cedar` file on disk is out of reach either way, and the
    guide says so.
    """
    module_level: dict[int, str] = {}
    for statement in context.tree.body:
        names: list[str] = []
        if isinstance(statement, ast.Assign):
            names = [t.id for t in statement.targets if isinstance(t, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names = [statement.target.id]
        value = getattr(statement, "value", None)
        if names and isinstance(value, ast.Constant):
            module_level[id(value)] = names[0]

    for node in ast.walk(context.tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text: str = node.value
        if "permit(" not in text and "forbid(" not in text:
            continue
        if "principal" not in text or ";" not in text:
            continue
        node_id = getattr(node, "_mutant_id", -1)
        scope = context.scopes.get(node_id, ())
        name = module_level.get(id(node))
        if not scope and name is None:
            continue

        offer = _cedar_offer(node.lineno, scope, node_id, name)
        if _CEDAR_FORBID.search(text):
            yield offer(
                "cedar.flip-effect",
                "a Cedar `forbid` turned into a `permit`",
                _CEDAR_FORBID.sub("permit(", text),
            )
        for match in _CEDAR_CONDITION.finditer(text):
            clause = match.group(0)
            yield offer(
                "cedar.drop-condition",
                f"the Cedar clause `{' '.join(clause.split())[:56]}`",
                text.replace(clause, "", 1),
            )
        statements = [s for s in text.split(";") if s.strip()]
        if len(statements) > 1:
            for index in range(len(statements)):
                yield offer(
                    "cedar.delete-policy",
                    f"the Cedar policy `{' '.join(statements[index].split())[:56]}`",
                    ";".join(s for i, s in enumerate(statements) if i != index) + ";",
                )


def _value_operators(context: _Context) -> Iterator[Candidate]:
    """Rebind a declared constant: a bound, a deny-list, a redaction pattern.

    These run at import time and so have no function to recompile; they are
    installed by rebinding the name in the defining module *and* in every module
    that imported it by value.
    """
    module = context.module
    if module is None:
        return
    for node in context.tree.body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            lowered = name.lower()
            live = getattr(module, name, None)
            if live is None:
                continue
            if (
                isinstance(live, int | float)
                and not isinstance(live, bool)
                and any(token in lowered for token in LIMIT_TOKENS)
                and live > 0
            ):
                yield Candidate(
                    operator="value.widen-bound",
                    control=f"the bound `{name} = {live}` (widened past reach)",
                    line=node.lineno,
                    scope=(),
                    kind="value",
                    value_path=(name,),
                    value=type(live)(_WIDE),
                )
            elif isinstance(live, re.Pattern) and any(
                token in lowered for token in REDACTION_TOKENS
            ):
                yield Candidate(
                    operator="value.disable-pattern",
                    control=f"the redaction pattern `{name}` (it now matches nothing)",
                    line=node.lineno,
                    scope=(),
                    kind="value",
                    value_path=(name,),
                    value=_NEVER,
                )
            elif isinstance(live, frozenset | set | tuple) and live and any(
                token in lowered for token in DENY_TOKENS
            ):
                yield Candidate(
                    operator="value.empty-denylist",
                    control=f"every entry in the deny-list `{name}`",
                    line=node.lineno,
                    scope=(),
                    kind="value",
                    value_path=(name,),
                    value=type(live)(),
                )


#: Every operator, in the order they are offered. `--operators` filters on the
#: prefix, so `--operators predicate` selects all three predicate operators.
OPERATORS: tuple[str, ...] = (
    "predicate.drop-operand",
    "predicate.always-true",
    "expression.take-branch",
    "comprehension.drop-clause",
    "guard.remove-raise",
    "guard.never-fires",
    "guard.always-fires",
    "guard.drop-statement",
    "declaration.drop-keyword",
    "declaration.widen-bound",
    "cedar.flip-effect",
    "cedar.drop-condition",
    "cedar.delete-policy",
    "value.widen-bound",
    "value.disable-pattern",
    "value.empty-denylist",
)


def scan(tree: ast.Module, module_name: str | None) -> list[Candidate]:
    """Every mutation this library can make to one already-tagged module."""
    module = sys.modules.get(module_name) if module_name else None
    context = _Context(module=module, tree=tree, scopes=tag(tree))
    found: list[Candidate] = []
    found.extend(_predicate_operators(context))
    found.extend(_declaration_operators(context))
    found.extend(_cedar_operators(context))
    found.extend(_value_operators(context))
    found.sort(key=lambda c: (c.line, c.operator, c.control))
    return found
