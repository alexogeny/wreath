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
from typing import Any, NamedTuple

#: A function is a candidate for the predicate operators when its own source
#: names one of these. The list is deliberately readable rather than clever: it
#: is the vocabulary of access control, secrecy, and bounds. Widening it costs
#: run time; narrowing it costs findings. It is printed by `--explain`.
CONTROL_TOKENS: frozenset[str] = frozenset(
    {
        "admin",
        "allow",
        "audience",
        "authent",
        "authoriz",
        "backup",
        "bound",
        "capab",
        "ceiling",
        "cert",
        "challenge",
        "claim",
        "cookie",
        "cors",
        "credential",
        "csrf",
        "decrypt",
        "deny",
        "digest",
        "encrypt",
        "escape",
        "expire",
        "exposure",
        "forbid",
        "grant",
        "guard",
        "hash",
        "hmac",
        "identif",
        "identity",
        "issuer",
        "leeway",
        "limit",
        "mfa",
        "nonce",
        "origin",
        "owner",
        "owns",
        "passcode",
        "password",
        "pending",
        "permiss",
        "permit",
        "policy",
        "policies",
        "principal",
        "privileg",
        "quota",
        "readonly",
        "redact",
        "refus",
        "reject",
        "replay",
        "requirement",
        "revoke",
        "role",
        "rotate",
        "sandbox",
        "sanitiz",
        "scope",
        "secret",
        "secure",
        "sensitive",
        "session",
        "signature",
        "skew",
        "sortable",
        "stamp",
        "tenant",
        "throttle",
        "token",
        "totp",
        "traversal",
        "trust",
        "unauthor",
        "verif",
        "webauthn",
        "withheld",
        "writable",
    }
)
_CONTROL_PATTERN = re.compile(
    "|".join(map(re.escape, sorted(CONTROL_TOKENS, key=len, reverse=True)))
)

#: Keywords that *are* a control when they appear at a call site. Dropping one
#: is the source-level spelling of "this control was never declared".
CONTROL_KEYWORDS: frozenset[str] = frozenset(
    {
        "action",
        "algorithms",
        "allow",
        "allow_list",
        "allowed",
        "audience",
        "auth",
        "authenticated",
        "authorize",
        "authorizer",
        "burst",
        "cancel_on_disconnect",
        "challenge",
        "cost",
        "csrf",
        "dependencies",
        "elicitation",
        "exempt",
        "expose",
        "http_only",
        "identify",
        "issuer",
        "key",
        "limit",
        "limits",
        "max_age",
        "middleware",
        "object_authorizer",
        "origins",
        "personal",
        "policies",
        "policy",
        "permissions",
        "rate_limit",
        "readonly",
        "require_user_verification",
        "entitlements",
        "requirement",
        "resource",
        "roles",
        "rp_id",
        "same_site",
        "sampling",
        "scope",
        "scopes",
        "second_factor",
        "secure",
        "sensitive",
        "skew",
        "sortable_fields",
        "subject",
        "verifier",
        "window",
        # The composed principal's controls (`wreath._auth.principal`). `scope=` and
        # `organizations=`/`entitlements=` are declarations in exactly the sense this
        # set means: dropping one is the source-level spelling of "the delegation
        # never had a scope" or "this application never wired memberships in", and
        # both are the mistake most likely to be made once and never noticed.
        # `ttl=` needs no entry -- `LIMIT_TOKENS` already carries it, so a
        # delegation's expiry is widened past reach by `declaration.widen-bound`.
        "organizations",
    }
)

#: Keywords and constant names that are numeric ceilings. Widening one to
#: `_WIDE` is the "limit that does not limit" mutation.
LIMIT_TOKENS: frozenset[str] = frozenset(
    {
        "burst",
        "capacity",
        "ceiling",
        "chunk",
        "cost",
        "deadline",
        "depth",
        "idle",
        "leeway",
        "limit",
        "max",
        "period",
        "quota",
        "rate",
        "seconds",
        "size",
        "skew",
        "timeout",
        "ttl",
        "window",
    }
)

#: What a widened bound becomes. Large enough that nothing reaches it, small
#: enough that arithmetic on it does not overflow a C `int` in the native paths.
_WIDE = 1 << 40

#: Names whose value is a deny-list: emptying one removes every refusal in it.
DENY_TOKENS: frozenset[str] = frozenset(
    {
        "banned",
        "blocked",
        "denied",
        "deny",
        "deferred",
        "disallowed",
        "forbidden",
        "refused",
        "reserved",
        "unsafe",
    }
)

#: Names whose value is a redaction pattern: a pattern that matches nothing
#: redacts nothing.
REDACTION_TOKENS: frozenset[str] = frozenset(
    {
        "sensitive",
        "secret",
        "redact",
        "private",
        "scrub",
        "mask",
    }
)

#: A regex that can never match anything, for `value.disable-pattern`.
_NEVER = re.compile(r"(?!x)x")

#: Function names that read as "may this caller do this?". Their bodies become
#: `return True`.
PREDICATE_TOKENS: frozenset[str] = frozenset(
    {
        "allow",
        "authoriz",
        "belongs",
        "can_",
        "check",
        "eligible",
        "entitled",
        "has_",
        "is_",
        "may_",
        "owns",
        "owned",
        "permit",
        "valid",
        "verif",
    }
)

#: Call targets whose result establishes who the caller is. Deleting the call
#: is how a control ends up keyed on something the caller mints for free.
ESTABLISH_TOKENS: frozenset[str] = frozenset(
    {
        "authenticate",
        "authorize",
        "check",
        "identify",
        "refresh",
        "resolve",
        "rotate",
        "touch",
        "validate",
        "verify",
    }
)

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


def _matches(names: set[str]) -> bool:
    return any(_CONTROL_PATTERN.search(name) is not None for name in names)


def _short(node: ast.AST, width: int = 72) -> str:
    try:
        text = ast.unparse(node)
    except AttributeError, ValueError, TypeError:  # pragma: no cover
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
    to remove unmutated. The answer lives one layer down.

    **Two layers down, and both are needed.** `RouteDefinition`'s defaulted
    fields are what the record carries, but not every keyword survives as a
    field: `permissions=` is folded into `requirement` by `Router.route` before
    the record is built, so reading the record alone made the one decorator
    keyword that demands a named permission invisible -- while `dependencies=`
    beside it was covered. `Router.route`'s own signature is the decorator's
    real vocabulary, so it is unioned in.
    """
    try:
        from dataclasses import MISSING, fields

        from ..router import RouteDefinition, Router
    except ImportError:  # pragma: no cover - wreath is always importable here
        return frozenset()
    names = {
        field.name
        for field in fields(RouteDefinition)
        if field.default is not MISSING or field.default_factory is not MISSING
    }
    declared = _defaulted_keywords(Router.route)
    return frozenset(names if declared is None else names | declared)


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


#: Call sites that *declare* controls but whose callee routinely cannot be
#: resolved, mapped to the keywords that are controls on them.
#:
#: `_resolve_callee` answers by walking attributes to a module global, so it
#: succeeds for `crud_router(...)` imported at the top of a factory and fails
#: for the two spellings applications actually reach for: `application.crud(...)`
#: where the receiver is a *parameter* (the camera-trap example's `mount`), and
#: `mcp.tool(...)` where the receiver is a *local* built inside the factory. In
#: both cases the operator declined for every keyword, so the newest
#: authorization surfaces in the framework were mutated not at all.
#:
#: This is the same argument `_route_metadata` already makes one layer down: the
#: name and the keyword together are specific enough to answer without resolving
#: the callee. It is a heuristic, exactly as `CONTROL_TOKENS` is, and it is
#: consulted *only* when resolution has already failed -- so a callee that can be
#: asked is still asked, and this table never overrides a real signature.
_DECLARING_CALLS: dict[str, frozenset[str]] = {
    # `crud_router(model, opener, ...)` and the `app.crud(...)` sugar for it.
    "crud": frozenset(
        {
            "authorize",
            "object_authorizer",
            "expose",
            "readonly",
            "exclude",
            "operations",
            "page_size",
        }
    ),
    "crud_router": frozenset(
        {
            "authorize",
            "object_authorizer",
            "expose",
            "readonly",
            "exclude",
            "operations",
            "page_size",
        }
    ),
    # `@mcp.tool(...)`, and the resource/prompt declarations beside it.
    "tool": frozenset(
        {
            "action",
            "resource",
            "rate_limit",
            "second_factor",
            "sampling",
            "elicitation",
        }
    ),
    "resource": frozenset({"action", "rate_limit", "second_factor"}),
    "prompt": frozenset({"action", "rate_limit", "second_factor"}),
    # `wreath.graphql`'s per-field declarations. `GraphQL(authorizer=...)` was
    # already reachable because the class is an imported module global, but
    # `api` is a local in every factory, so the *policy per field* -- which is
    # the whole of GraphQL's authorization vocabulary -- resolved to nothing.
    # `cost=` rides along because a complexity weight is a bound, and a field
    # whose weight falls back to 1 is a field that no longer counts.
    "field": frozenset({"policy", "cost"}),
    "query": frozenset({"policy", "cost"}),
    "mutation": frozenset({"policy", "cost"}),
}

#: `wreath.grpc`'s four method shapes. `service.unary(...)` is not route-shaped to
#: `_looks_like_a_route` (no verb, no literal `/`-path) and its receiver is a
#: local, so both branches declined and a gRPC method's guards were mutated not
#: at all.
#:
#: The keywords are listed rather than taken from `_route_metadata()`, which is
#: the wrong source here: that reads `RouteDefinition`'s *dataclass fields*, and
#: these reach the route **decorator**, whose vocabulary is wider. `GrpcService.
#: router`'s own docstring names the contract -- "metadata passed to a method
#: decorator reaches `RouteDefinition` unchanged, so `roles=`, `dependencies=`
#: and `rate_limit=` are enforced by the same tape as any REST route".
_GRPC_METHOD_CONTROLS: frozenset[str] = frozenset(
    {
        "action",
        "authorize",
        "dependencies",
        "middleware",
        "permissions",
        "rate_limit",
        "requirement",
        "roles",
        "second_factor",
    }
)

for _grpc_call in ("unary", "server_stream", "client_stream", "bidi"):
    _DECLARING_CALLS[_grpc_call] = _GRPC_METHOD_CONTROLS
del _grpc_call


def _declared_controls(node: ast.Call) -> frozenset[str] | None:
    """The control keywords for a declaring call whose callee did not resolve."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name is None:
        return None
    return _DECLARING_CALLS.get(name)


def _defaulted_keywords(callee: Any) -> frozenset[str] | None:
    try:
        signature = inspect.signature(callee)
    except TypeError, ValueError:
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


def _drop_mapping_entry(kw_index: int, key_index: int) -> Callable[[ast.AST], ast.AST]:
    """Remove one key from a mapping passed as a keyword.

    The per-operation half of `authorize={...}`: dropping the whole keyword
    removes every operation's control at once, and a suite that exercises any
    one of them kills that mutant while the rest stay unverified.
    """

    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        mapping = _want(call.keywords[kw_index].value, ast.Dict)
        mapping.keys = [k for i, k in enumerate(mapping.keys) if i != key_index]
        mapping.values = [v for i, v in enumerate(mapping.values) if i != key_index]
        return call

    return mutate


def _widen_mapping_entry(
    kw_index: int, key_index: int, method: str
) -> Callable[[ast.AST], ast.AST]:
    """Rewrite one mapping entry's `Access.deny()` into `Access.public()`.

    The receiver expression is *reused* rather than rebuilt from a name, so the
    mutant introduces no identifier the module might not have imported. A
    mutation that raises `NameError` is a broken mutant, and a broken mutant
    that kills a test inflates the score with something the suite did not catch.
    """

    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        mapping = _want(call.keywords[kw_index].value, ast.Dict)
        inner = _want(mapping.values[key_index], ast.Call)
        attribute = _want(inner.func, ast.Attribute)
        mapping.values[key_index] = ast.Call(
            func=ast.Attribute(value=attribute.value, attr=method, ctx=ast.Load()),
            args=[],
            keywords=[],
        )
        return call

    return mutate


def _drop_sequence_element(kw_index: int, element: int) -> Callable[[ast.AST], ast.AST]:
    """Remove one entry from a tuple/list passed as a keyword.

    `readonly=("id", "created_at")` is one control per column, so dropping the
    keyword makes every column writable in a single mutant. One at a time names
    which column nobody checks.
    """

    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        sequence = call.keywords[kw_index].value
        if not isinstance(sequence, ast.Tuple | ast.List):  # pragma: no cover
            raise TypeError(f"expected a sequence, got {type(sequence).__name__}")
        sequence.elts = [e for i, e in enumerate(sequence.elts) if i != element]
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


# the operators


def _control_functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = _names_in(node)
            names.add(node.name.lower())
            if _matches(names):
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
                        control=f"clause `{_short(operand, 56)}` in a compound {joiner} condition",
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
                # A parenthesised condition spread over several lines leaves the
                # `if` line carrying no bytecode at all -- the first thing that
                # executes is the first operand, one line down -- so watching
                # `node.lineno` alone reported an exercised guard as UNREACHED.
                # That is the has-nothing-to-check failure mode with the tool as its subject,
                # the same one the `def`-line rule exists for; `users.py`'s
                # step-up check was covered by five tests and read as unwatched.
                guard_watch = (node.test.lineno,)
                yield Candidate(
                    operator="guard.never-fires",
                    control=f"the guarded branch `if {_short(node.test, 56)}`",
                    line=node.lineno,
                    scope=scope,
                    node_id=node_id,
                    watch=guard_watch,
                    mutate=_replace_test(False),
                )
                if not node.orelse:
                    yield Candidate(
                        operator="guard.always-fires",
                        control=f"the condition on `if {_short(node.test, 56)}`",
                        line=node.lineno,
                        scope=scope,
                        node_id=node_id,
                        watch=guard_watch,
                        mutate=_replace_test(True),
                    )

            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Await | ast.Call):
                call = node.value.value if isinstance(node.value, ast.Await) else node.value
                if not isinstance(call, ast.Call):
                    continue
                target = call.func
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
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
        if defaults is None:
            # The same argument for the other declaring call sites: a crud
            # router mounted off a parameter, an MCP tool registered on a local.
            defaults = _declared_controls(node)
        label = _short(node.func, 40)
        yield from _mapping_entry_operators(node, node_id, scope, label, defaults)
        yield from _expose_operators(context.module, node, node_id, scope, label, defaults)
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


#: Keywords whose value is a per-operation mapping of controls. `personal=` on
#: a `privacy.classify(...)` is one: each entry is a column somebody declared as
#: the subject's data, and dropping one is exactly the erasure that reports
#: success while leaving a column behind.
_MAPPING_CONTROLS: frozenset[str] = frozenset({"authorize", "personal", "policies"})

#: Keywords whose value is a sequence of column names, each one its own control.
_SEQUENCE_CONTROLS: frozenset[str] = frozenset({"readonly", "exclude"})

#: The permissive twin of a refusal. `Access.deny()` answers 403 with the route
#: present; `Access.public()` is the same declaration with the refusal taken out.
_PERMISSIVE_ACCESS = "public"

#: The method that spells an outright refusal, as opposed to a narrowing.
#: `Access.deny()` and `Access.roles("reader")` are the same *transform* --
#: rewrite the method to `public` -- but they are not the same *finding*, which
#: is why they are two operators rather than one:
#:
#: * `crud.permit-refused-operation` removes a rule that said **nobody**. A
#:   survivor there means no test ever checked that the operation is refused,
#:   and the operation is now reachable by anyone.
#: * `crud.widen-access` removes a rule that said **these roles**. A survivor
#:   means no test distinguished a permitted caller from a refused one.
#:
#: One name for both read as a single number in the report, and the first is
#: the more serious of the two by a distance. The transform is shared; only the
#: label and the operator name differ.
_REFUSAL_ACCESS = "deny"


def _add_sequence_element(kw_index: int, value: str) -> Callable[[ast.AST], ast.AST]:
    """Append one name to a tuple/list passed as a keyword."""

    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        sequence = call.keywords[kw_index].value
        if not isinstance(sequence, ast.Tuple | ast.List):  # pragma: no cover
            raise TypeError(f"expected a sequence, got {type(sequence).__name__}")
        sequence.elts = [*sequence.elts, ast.Constant(value=value)]
        return call

    return mutate


def _add_keyword(name: str, value: str) -> Callable[[ast.AST], ast.AST]:
    """Add a keyword the call site does not have, holding one name."""

    def mutate(node: ast.AST) -> ast.AST:
        call = _want(node, ast.Call)
        call.keywords = [
            *call.keywords,
            ast.keyword(
                arg=name, value=ast.Tuple(elts=[ast.Constant(value=value)], ctx=ast.Load())
            ),
        ]
        return call

    return mutate


def _expose_operators(
    module: ModuleType | None,
    node: ast.Call,
    node_id: int,
    scope: tuple[str, ...],
    label: str,
    defaults: frozenset[str] | None,
) -> Iterator[Candidate]:
    """Reveal one column `crud` withholds by default, one mutant per column.

    **The name a mutation has to add is exactly the one not written at the call
    site**, which is why this was declined as out of static reach: `expose=` is
    the escape hatch for columns withheld *by default*, so the operator has to
    know what the default hides. Fabricating a name either raises (killing the
    mutant for a reason unrelated to any control) or is ignored (reporting a
    survivor nobody can act on).

    It is knowable after all, and from the same place everything else here is
    knowable from: the **model**. `crud_router(Sighting, ...)` names it as its
    first argument, `_resolve_callee` already walks a module global to a live
    object, and `wreath.crud.sensitive_fields` is the declaration of what is
    withheld -- read, not guessed. So the operator fires only where the model
    resolves, and declines silently where it does not, which is the same rule
    the keyword operators follow.

    A column already named in `expose=` is skipped: revealing what is already
    revealed is a no-op mutant, and a no-op mutant that survives is noise.

    `retrieval_fields` is deliberately *not* included. A `Vector` embedding is
    withheld because it is infrastructure rather than because it is a secret,
    so exposing one is a payload-size decision and not an authorization one --
    and this library's whole subject is controls.
    """
    if defaults is None or "expose" not in defaults or not node.args:
        return
    model = _resolve_callee(module, node.args[0])
    # The precondition rather than a `try`: a first argument that resolves to
    # something which is not a wreath model is the ordinary case for a call
    # this table matched by name alone, so it is a check and not an exception.
    if not isinstance(model, type) or not hasattr(model, "__wreath_column_map__"):
        return
    from ..crud import sensitive_fields

    withheld = sensitive_fields(model)
    if not withheld:
        return
    index = next((i for i, k in enumerate(node.keywords) if k.arg == "expose"), None)
    already: set[str] = set()
    if index is not None:
        value = node.keywords[index].value
        if not isinstance(value, ast.Tuple | ast.List):
            return
        already = {
            element.value
            for element in value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    for column in sorted(withheld - already):
        yield Candidate(
            operator="crud.expose-sensitive",
            control=f"`{column}` is withheld by `{label}(...)` "
            f"(exposed, so it reaches every response)",
            line=node.lineno,
            scope=scope,
            node_id=node_id,
            watch=(node.lineno, node.func.lineno),
            mutate=(
                _add_sequence_element(index, column)
                if index is not None
                else _add_keyword("expose", column)
            ),
        )


def _mapping_entry_operators(
    node: ast.Call,
    node_id: int,
    scope: tuple[str, ...],
    label: str,
    defaults: frozenset[str] | None,
) -> Iterator[Candidate]:
    """Per-entry operators for controls declared as a mapping or a sequence.

    `authorize={"list": ..., "create": Access.deny()}` is four controls wearing
    one keyword. Removing the keyword removes all of them at once, so any test
    that exercises any operation kills the mutant and the rest are reported as
    covered without ever having been checked.
    """
    if defaults is None:
        return
    for index, keyword in enumerate(node.keywords):
        name = keyword.arg
        if name is None or name not in defaults:
            continue
        value = keyword.value

        if name in _MAPPING_CONTROLS and isinstance(value, ast.Dict):
            for position, key in enumerate(value.keys):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                operation = key.value
                yield Candidate(
                    operator="crud.drop-operation-authorize",
                    control=f"the `{operation}` entry of `{name}=` on "
                    f"`{label}(...)` (that operation falls back)",
                    line=getattr(key, "lineno", node.lineno),
                    scope=scope,
                    node_id=node_id,
                    watch=(node.lineno, node.func.lineno),
                    mutate=_drop_mapping_entry(index, position),
                )
                entry = value.values[position]
                if isinstance(entry, ast.Call) and isinstance(entry.func, ast.Attribute):
                    if entry.func.attr == _PERMISSIVE_ACCESS:
                        continue
                    refusal = entry.func.attr == _REFUSAL_ACCESS
                    yield Candidate(
                        operator=(
                            "crud.permit-refused-operation" if refusal else "crud.widen-access"
                        ),
                        control=(
                            f"the `{operation}` refusal `{_short(entry, 32)}` "
                            f"(turned into a `{_PERMISSIVE_ACCESS}` permit)"
                            if refusal
                            else f"the `{operation}` rule `{_short(entry, 32)}` "
                            f"(widened to `{_PERMISSIVE_ACCESS}`)"
                        ),
                        line=getattr(entry, "lineno", node.lineno),
                        scope=scope,
                        node_id=node_id,
                        watch=(node.lineno, node.func.lineno),
                        mutate=_widen_mapping_entry(index, position, _PERMISSIVE_ACCESS),
                    )

        elif name in _SEQUENCE_CONTROLS and isinstance(value, ast.Tuple | ast.List):
            for position, element in enumerate(value.elts):
                if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                    continue
                yield Candidate(
                    operator="crud.unprotect-column",
                    control=f"`{element.value}` in `{name}=` on `{label}(...)` "
                    f"(that column alone loses its protection)",
                    line=getattr(element, "lineno", node.lineno),
                    scope=scope,
                    node_id=node_id,
                    watch=(node.lineno, node.func.lineno),
                    mutate=_drop_sequence_element(index, position),
                )


_CEDAR_FORBID = re.compile(r"\bforbid\s*\(")
_CEDAR_CONDITION = re.compile(r"\b(when|unless)\s*\{[^{}]*\}", re.DOTALL)


class _CedarMasks(NamedTuple):
    """Two per-character views of one policy source.

    `code` is what the operators may key on -- punctuation and keywords outside
    both commentary and string literals. `uncommented` keeps the literals, and
    is what a mutant's label is built from, so the report names
    `permit(principal in Role::"ranger", ...)` rather than a policy with every
    quoted id blanked out.
    """

    code: tuple[bool, ...]
    uncommented: tuple[bool, ...]


def _cedar_code_mask(text: str) -> _CedarMasks:
    """Per character, whether it is Cedar code rather than a comment or a string.

    Cedar policy sets are written with `//` commentary and quoted literals, and
    both may contain the punctuation this module keys on. That is not
    hypothetical: `example/camera_trap/policies.py` has the comment *"Researchers
    hold a permit; rangers are the people who respond."*, and splitting the
    source on a bare `;` cut it in half -- producing one mutant that deleted a
    sentence rather than a policy and another that did not parse at all.

    Neither was visible until `PolicyPatch` made these mutations reach the
    compiled engine, because a mutation that changed only a rebound string
    survived whatever it said.
    """
    length = len(text)
    code = [True] * length
    uncommented = [True] * length
    index = 0
    while index < length:
        character = text[index]
        if character == '"':
            code[index] = False
            index += 1
            while index < length:
                code[index] = False
                if text[index] == "\\" and index + 1 < length:
                    code[index + 1] = False
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if character == "/" and text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end < 0 else end
            for position in range(index, end):
                code[position] = False
                uncommented[position] = False
            index = end
            continue
        index += 1
    return _CedarMasks(tuple(code), tuple(uncommented))


def _cedar_masked_text(text: str, mask: tuple[bool, ...], start: int, end: int) -> str:
    """`text[start:end]` with everything `mask` excludes removed."""
    return "".join(text[i] for i in range(start, end) if mask[i])


def _cedar_statements(text: str, mask: tuple[bool, ...]) -> list[tuple[int, int]]:
    """Half-open spans of each policy statement, ending at its own `;`.

    Text after the final `;` is whitespace or trailing commentary and is not a
    statement, so it is never offered for deletion.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for index, character in enumerate(text):
        if character == ";" and mask[index]:
            spans.append((start, index + 1))
            start = index + 1
    return spans


def _cedar_offer(
    line: int, scope: tuple[str, ...], node_id: int, name: str | None
) -> Callable[[str, str, str], Candidate]:
    """A policy inside a function is recompiled; one at module level is rebound."""

    def offer(operator: str, control: str, replacement: str) -> Candidate:
        if scope:
            return Candidate(
                operator=operator,
                control=control,
                line=line,
                scope=scope,
                node_id=node_id,
                mutate=_rewrite_string(replacement),
            )
        return Candidate(
            operator=operator,
            control=control,
            line=line,
            scope=(),
            kind="value",
            value_path=(name or "",),
            value=replacement,
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

        masks = _cedar_code_mask(text)
        mask = masks.code
        offer = _cedar_offer(node.lineno, scope, node_id, name)
        forbids = [m for m in _CEDAR_FORBID.finditer(text) if mask[m.start()]]
        if forbids:
            flipped = text
            for match in reversed(forbids):
                flipped = flipped[: match.start()] + "permit(" + flipped[match.end() :]
            yield offer(
                "cedar.flip-effect",
                "a Cedar `forbid` turned into a `permit`",
                flipped,
            )
        for match in _CEDAR_CONDITION.finditer(text):
            if not mask[match.start()]:
                continue
            clause = match.group(0)
            yield offer(
                "cedar.drop-condition",
                f"the Cedar clause `{' '.join(clause.split())[:56]}`",
                text[: match.start()] + text[match.end() :],
            )
        spans = _cedar_statements(text, mask)
        policies = [
            (start, end)
            for start, end in spans
            if any(
                keyword in _cedar_masked_text(text, mask, start, end)
                for keyword in ("permit", "forbid")
            )
        ]
        if len(policies) > 1:
            for start, end in policies:
                body = " ".join(_cedar_masked_text(text, masks.uncommented, start, end).split())
                yield offer(
                    "cedar.delete-policy",
                    f"the Cedar policy `{body[:56]}`",
                    text[:start] + text[end:],
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
            elif (
                isinstance(live, frozenset | set | tuple)
                and live
                and any(token in lowered for token in DENY_TOKENS)
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
    "crud.drop-operation-authorize",
    "crud.widen-access",
    "crud.permit-refused-operation",
    "crud.unprotect-column",
    "crud.expose-sensitive",
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
