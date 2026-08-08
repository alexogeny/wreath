"""Middleware contracts and the startup compilation that turns them into a tape.

Wreath accepts middleware in two forms. The *hook* form is an object carrying
`before` or `before_sync` and/or `after`, `after_sync`, or `after_inplace`
attributes;
`MiddlewareHooks` is the canonical container, but any object with those
attributes qualifies; standard HTTP policy is configured separately through `HttpPolicy`.
The *legacy* form is the familiar `async def (request, call_next)` callable. An
object carrying both is rejected with `TypeError` at compile time, because
which form it is cannot be guessed.

Hook middleware is compiled once, at startup, into a `MiddlewareTape`: a flat
tuple of instructions with precomputed jump targets, walked by one loop instead
of a stack of nested closures. A contiguous run of `before_sync` hooks is fused
into a single instruction that runs them in one synchronous pass;
`after_sync` instructions likewise run without a coroutine or await, while
`after_inplace` additionally promises not to replace the response. One legacy
middleware anywhere in a route's chain opts that whole route out of the tape:
the chain falls back to nested closures, with the hook middleware adapted into
that shape.

Setting `global_scope = True` on a middleware object makes `Wreath.add_middleware`
route it to `Wreath.add_global_middleware` instead of a route's chain. Global hooks
run around routing itself, so they also cover route misses, static files, and
authentication and authorization failures, and their `after` hooks are handed
error responses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ..request import Request

type ResponseValue = Any
type CallNext = Callable[[Request], Awaitable[ResponseValue]]
type BeforeHook = Callable[[Request], Awaitable[ResponseValue | None]]
# A synchronous before hook -- a plain function, not a coroutine. Contiguous
# sync hooks are fused into one instruction run in a single pass, with no
# per-hook coroutine or await.
type SyncBeforeHook = Callable[[Request], ResponseValue | None]
type AfterHook = Callable[[Request, ResponseValue], Awaitable[ResponseValue]]
# The egress twin of ``SyncBeforeHook``. A plain response transformer whose
# result is consumed directly, without allocating and awaiting a coroutine.
type SyncAfterHook = Callable[[Request, ResponseValue], ResponseValue]
# A response mutator. Dispatch preserves the object it was handed, so this
# form pays neither coroutine machinery nor response coercion after the call.
type InPlaceAfterHook = Callable[[Request, ResponseValue], None]
type RoutePredicate = Callable[[MiddlewareRoute], bool]


@dataclass(frozen=True, slots=True)
class MiddlewareRoute:
    """The static route facts an `applies_to` predicate is given.

    One instance is built per route and method during startup compilation and
    passed to every candidate middleware's `applies_to`. Nothing here varies per
    request, and that is the point: the decision is made once, and the
    middleware is either present in that route's compiled chain or absent from
    it, with no per-request predicate call.

    Args:
        path: The registered route template, parameter placeholders intact.
        method: The HTTP method, uppercased at registration.
        endpoint: The handler function as registered, before argument binding.
        authenticated: True when the route declares an authentication requirement.
    """

    path: str
    method: str
    endpoint: CallNext
    authenticated: bool = False


#: The closed vocabulary a contract may declare, and the only strings a
#: generated client is allowed to act on. Closed on purpose: an unknown
#: behaviour reaching a client is a typo that silently stops it sending an
#: idempotency key, and that failure is invisible until a retry duplicates a
#: write. `generate_openapi` refuses anything not named here.
#:
#: * `idempotency-key` -- send a key on the unsafe methods; reuse it on retry.
#: * `retry-after` -- wait the header on a 429/503 rather than backing off blind.
#: * `etag` -- retain the `ETag`, send `If-None-Match`, treat 304 as a hit.
#: * `csrf-token` -- read the token where the middleware says and send it on
#:   the unsafe methods.
BEHAVIOURS: frozenset[str] = frozenset(
    {"idempotency-key", "retry-after", "etag", "csrf-token"}
)


@dataclass(frozen=True, slots=True)
class HeaderSpec:
    """One header a middleware reads or emits.

    Args:
        name: The header name in its canonical casing, for the document.
        description: What it is for, rendered into the operation.
        required: True when the middleware refuses a request without it.
        const: The exact value, when configuration fixes it. `RateLimit-Policy`
            is `60;w=60` for a middleware built with `limit=60, window=60.0`,
            and putting that in the document is what lets a test assert the
            document and the runtime agree rather than merely resemble.
    """

    name: str
    description: str = ""
    required: bool = False
    const: str | None = None


@dataclass(frozen=True, slots=True)
class MiddlewareContract:
    """What a middleware adds to every operation it covers.

    Returned by an optional `describe()`. The OpenAPI generator collects these
    by *asking* every middleware on a route's tape, the same way
    `Wreath.schema_components` asks for `component()` -- a hand-kept list would
    be one more place to forget a new middleware, and forgetting is the defect
    the mechanism exists to remove.

    A contract describes what the middleware was *configured* to do, not what
    its class can do in general, so the document and the runtime cannot
    disagree about a limit or a policy string.

    Args:
        request_headers: Headers the middleware reads, as header parameters.
        response_headers: `(status, header)` pairs. A status of `None` means
            the operation's own success status, so `ETag` can be declared
            without knowing whether the route answers 200 or 201.
        responses: `(status, ResponseSpec | model)` pairs for the statuses this
            middleware can answer on its own, in `RouteDefinition.responses`
            shape so `ResponseSpec` is reused rather than duplicated.
        methods: Restrict the contract to these uppercased methods; `None`
            covers every method the route serves. An `Idempotency-Key` belongs
            on the unsafe methods and nowhere else.
        behaviours: Names from `BEHAVIOURS` that a generated client may act on.
    """

    request_headers: tuple[HeaderSpec, ...] = ()
    response_headers: tuple[tuple[int | None, HeaderSpec], ...] = ()
    responses: tuple[tuple[int, Any], ...] = ()
    methods: frozenset[str] | None = None
    behaviours: frozenset[str] = frozenset()


class DescribesItself(Protocol):
    """A middleware that declares what it adds to the operations it covers."""

    def describe(self) -> MiddlewareContract: ...


class LegacyMiddleware(Protocol):
    """The nested `(request, call_next)` middleware form.

    Await `call_next(request)` to continue down the chain and return what it
    gives you, or return a response without calling it to short-circuit. This
    form is the one to reach for when the endpoint call itself must be wrapped
    -- a `try`/`finally`, a context manager, a timeout. It costs the tape: a
    single legacy middleware makes the whole route compile to nested closures.
    Everything else is better expressed as `MiddlewareHooks`.

    An object that defines `__call__` *and* any of `before`, `before_sync`,
    `after`, `after_sync`, or `after_inplace` is rejected with `TypeError` when
    the route compiles.
    """

    async def __call__(self, request: Request, call_next: CallNext) -> ResponseValue: ...


@dataclass(frozen=True, slots=True)
class MiddlewareHooks:
    """A middleware expressed as hooks, so it can be fused into a linear tape.

    A `before` hook returns `None` to continue to the next hook, or a response
    value to short-circuit: the endpoint and every hook below are skipped. An
    `after` hook receives the response -- from the endpoint, from a
    short-circuit, or from the error handler -- and returns the response to
    carry on with, which is normally the one it was handed.

    `before_sync` is the synchronous form of `before`. It is a plain function,
    not a coroutine function, and a contiguous run of `before_sync` hooks is
    compiled into one instruction that calls them in a single pass, with no
    per-hook coroutine and no await. Set `before` or `before_sync`, never both;
    when both are set `before_sync` wins and `before` is silently unreachable.
    `after_sync` is the equivalent synchronous form of `after`; set one or the
    other, and `after_sync` wins when both exist.
    `after_inplace` is for a hook that only mutates the response. It returns
    nothing and takes precedence over both transforming forms.

    **When `after` runs.** An `after` hook runs only when its own `before`
    completed, or when the middleware has no `before` at all -- but it may run
    for a request that never reached the endpoint, and it may be handed an error
    response. Returning a response from `before` is a *completed* `before`, so
    that middleware keeps its `after`. Raising is not, so it does not. In a
    route-scoped tape a `before` that raises propagates out of the tape and no
    `after` hook runs at all; registered globally, the hooks whose `before`
    already completed still unwind and see the error response.

    Args:
        before: Async hook run on the way in; returns None or a response.
        after: Async hook run on the way out; returns the response to continue with.
        applies_to: Predicate over `MiddlewareRoute` deciding route membership.
        before_sync: The synchronous, fusible form of `before`.
        after_sync: The synchronous, non-awaiting form of `after`.
        after_inplace: Synchronous response mutation with no replacement value.
        contract: What this middleware adds to the OpenAPI operations it covers.
            `None` declares nothing, which is what an unannotated middleware
            has always done.
    """

    before: BeforeHook | None = None
    after: AfterHook | None = None
    applies_to: RoutePredicate | None = None
    before_sync: SyncBeforeHook | None = None
    after_sync: SyncAfterHook | None = None
    after_inplace: InPlaceAfterHook | None = None
    contract: MiddlewareContract | None = None

    def describe(self) -> MiddlewareContract | None:
        """The declared contract, or None when this middleware declares nothing."""
        return self.contract


@dataclass(frozen=True, slots=True)
class PipelineHooks:
    """Global hooks placed at the named boundaries of the request pipeline.

    `global_scope` is always True, so `Wreath.add_middleware` registers these
    globally: `before` and the selected egress hook bracket every HTTP request,
    including route misses, static files, and error responses. The four stage
    hooks in between fire only when the request reaches that boundary, which is
    what makes them cheap -- a stage nobody registered is not dispatched at all.

    Every hook returns `None` to continue or a response to terminate the request
    there. A stage hook that raises is converted to an error response rather
    than propagating.

    `after` runs only when this object's `before` completed, or when it has no
    `before`; a `before` that returns a response counts as completed, a `before`
    that raises does not. A terminating stage hook does not change that: `after`
    still runs, and is handed the stage's response. So `after` sees requests that
    never reached a handler, and it sees error responses.

    Args:
        before: Ingress, before routing. Every HTTP request passes through it.
        miss: No route matched, before the static-file and preflight fallbacks.
        pre_auth: A protected route matched and the caller is not yet identified.
        identity: The backend has run and `request.identity` is set.
        action: Authorization passed; the handler is about to be invoked.
        after: Egress, wrapping every response this middleware was entered for.
        after_sync: Synchronous egress; takes precedence over `after`.
        after_inplace: In-place egress; takes precedence over both other forms.
    """

    before: BeforeHook | None = None
    miss: BeforeHook | None = None
    pre_auth: BeforeHook | None = None
    identity: BeforeHook | None = None
    action: BeforeHook | None = None
    after: AfterHook | None = None
    after_sync: SyncAfterHook | None = None
    after_inplace: InPlaceAfterHook | None = None

    @property
    def global_scope(self) -> bool:
        """Always True. `Wreath.add_middleware` registers this as global middleware."""
        return True


type Middleware = LegacyMiddleware | MiddlewareHooks | PipelineHooks


@dataclass(frozen=True, slots=True)
class _BeforeInstruction:
    hook: BeforeHook
    failure_target: int
    sync: bool = False


@dataclass(frozen=True, slots=True)
class _FusedBeforeInstruction:
    """A contiguous run of synchronous before hooks, run in one pass. Each
    `(hook, failure_target)` pair short-circuits to its own target -- the
    after-region position of its middleware -- so entered after hooks still
    run correctly when a fused hook responds."""

    pairs: tuple[tuple[SyncBeforeHook, int], ...]


@dataclass(frozen=True, slots=True)
class _EndpointInstruction:
    endpoint: CallNext


@dataclass(frozen=True, slots=True)
class _AfterInstruction:
    hook: AfterHook


@dataclass(frozen=True, slots=True)
class _SyncAfterInstruction:
    hook: SyncAfterHook


@dataclass(frozen=True, slots=True)
class _InPlaceAfterInstruction:
    hook: InPlaceAfterHook


type _Instruction = (
    _BeforeInstruction
    | _FusedBeforeInstruction
    | _EndpointInstruction
    | _AfterInstruction
    | _SyncAfterInstruction
    | _InPlaceAfterInstruction
)
_UNSET = object()


#: Instruction opcodes, decided once at compile time. The dispatch loop used to
#: re-derive them per instruction per request with an `isinstance` ladder --
#: one boundary crossing per test, up to three per instruction. The kind of an
#: instruction is fixed when the tape is compiled, so the ladder was re-deriving
#: a constant on every request.
#:
#: Not visible in docs/agents/request-boundary-baseline.json: the traced sample
#: app registers global middleware, which runs from `_global_hooks` rather than
#: a route tape, so that scenario never enters this loop. The
#: `middleware-tape-mixed-dispatch` complexity probe covers it instead.
_OP_FUSED_BEFORE = 0
_OP_BEFORE = 1
_OP_ENDPOINT = 2
_OP_AFTER = 3
_OP_SYNC_AFTER = 4
_OP_INPLACE_AFTER = 5

_OPCODES: tuple[tuple[type, int, str], ...] = (
    (_FusedBeforeInstruction, _OP_FUSED_BEFORE, "fused_before"),
    (_BeforeInstruction, _OP_BEFORE, "before"),
    (_EndpointInstruction, _OP_ENDPOINT, "endpoint"),
    (_AfterInstruction, _OP_AFTER, "after"),
    (_SyncAfterInstruction, _OP_SYNC_AFTER, "after_sync"),
    (_InPlaceAfterInstruction, _OP_INPLACE_AFTER, "after_inplace"),
)


def _opcode(instruction: _Instruction) -> tuple[int, str]:
    for kind, code, name in _OPCODES:
        if isinstance(instruction, kind):
            return code, name
    raise TypeError(f"unknown middleware instruction: {instruction!r}")


class MiddlewareTape:
    """One immutable, linear middleware program compiled for a single route.

    Built by `compile_middleware` when every middleware on a route is in the
    hook form; not constructed directly, and the instruction type it takes is
    private. It is the compiled artifact you inspect, not one you assemble.

    Calling the tape runs one request through it. Instructions are laid out in
    execution order -- the before-region, the endpoint, then the after-region in
    reverse registration order -- and each before instruction carries the
    position to jump to when it short-circuits, so a response returned from a
    `before` lands directly on the first `after` that must still run. Every
    branch the loop takes was decided at compile time; the dispatch is on an
    integer opcode, not a chain of `isinstance` tests.

    The tape guarantees the response it returns came from an endpoint or from a
    short-circuiting `before`, and that every `after` whose own `before`
    completed was applied to it in reverse order. It does *not* catch
    exceptions: a hook or endpoint that raises propagates out of the tape, and
    no `after` runs for it. That is deliberate -- the application's exception
    handlers turn it into a response, and global middleware still unwinds
    around the result.

    `operations` holds the opcode name of each instruction in order -- one of
    `fused_before`, `before`, `endpoint`, `after`, `after_sync`,
    `after_inplace` -- which is how a test or a complexity probe asserts what
    the compiler produced.
    """

    __slots__ = ("_program", "operations")

    def __init__(self, instructions: tuple[_Instruction, ...]) -> None:
        decoded = tuple(_opcode(instruction) for instruction in instructions)
        # (opcode, instruction) pairs -- the executable form. The instruction is
        # typed `Any` deliberately: the opcode is the discriminator, and it is
        # what the dispatch loop below branches on. A type checker cannot narrow
        # on an int tag, and the alternatives both cost per instruction per
        # request -- re-testing with `isinstance` (what this replaced) or calling
        # `cast`, which is a real function call at runtime, not a no-op.
        self._program: tuple[tuple[int, Any], ...] = tuple(
            (code, instruction)
            for (code, _name), instruction in zip(decoded, instructions, strict=True)
        )
        self.operations = tuple(name for _code, name in decoded)

    async def __call__(self, request: Request) -> ResponseValue:
        """Run one request through the program and return its response.

        Raises:
            RuntimeError: The program ran to completion without producing a response.
        """
        program = self._program
        count = len(program)
        response: ResponseValue = _UNSET
        position = 0
        while position < count:
            opcode, instruction = program[position]
            if opcode == _OP_BEFORE:
                candidate = await instruction.hook(request)
                if candidate is None:
                    position += 1
                else:
                    response = candidate
                    position = instruction.failure_target
            elif opcode == _OP_FUSED_BEFORE:
                # One synchronous pass over the run: no coroutine, no await.
                position += 1
                for hook, failure_target in instruction.pairs:
                    candidate = hook(request)
                    if candidate is not None:
                        response = candidate
                        position = failure_target
                        break
            elif opcode == _OP_ENDPOINT:
                response = await instruction.endpoint(request)
                position += 1
            elif opcode == _OP_AFTER:
                response = await instruction.hook(request, response)
                position += 1
            elif opcode == _OP_SYNC_AFTER:
                response = instruction.hook(request, response)
                position += 1
            else:
                instruction.hook(request, response)
                position += 1
        if response is _UNSET:
            raise RuntimeError("middleware tape completed without a response")
        return response


_HOOK_ATTRIBUTES = (
    "before", "before_sync", "after", "after_sync", "after_inplace"
)


def _is_fused(middleware: Middleware) -> bool:
    if isinstance(middleware, MiddlewareHooks):
        return True
    hooks = any(hasattr(middleware, attribute) for attribute in _HOOK_ATTRIBUTES)
    if not hooks:
        return False
    # A hook middleware and a legacy `(request, call_next)` middleware are told
    # apart by which attributes they carry, so an object carrying both is
    # ambiguous -- and the ambiguity used to resolve silently in favour of the
    # hooks, which meant a legacy middleware that happened to define `after`
    # never had its `__call__` invoked at all. Nothing in the response said so.
    if callable(middleware) and not isinstance(middleware, type):
        raise TypeError(
            f"{type(middleware).__name__} defines both __call__ and "
            f"{', '.join(a for a in _HOOK_ATTRIBUTES if hasattr(middleware, a))}; "
            "a middleware is either the legacy (request, call_next) form or the "
            "hook form, and which one this is cannot be guessed. Remove one."
        )
    return True


def _fuse_sync_befores(instructions: list[_Instruction]) -> list[_Instruction]:
    """Collapse each contiguous run of synchronous before instructions into one
    `_FusedBeforeInstruction` and remap every jump position to the new layout.

    Failure targets only ever point into the after-region (>= endpoint), never
    back into the before-region, so fusing the before-region shifts those
    positions but never invalidates them.
    """
    grouped: list[_Instruction] = []
    remap: dict[int, int] = {}
    index = 0
    count = len(instructions)
    while index < count:
        instruction = instructions[index]
        if isinstance(instruction, _BeforeInstruction) and instruction.sync:
            new_position = len(grouped)
            pairs: list[tuple[SyncBeforeHook, int]] = []
            while (
                index < count
                and isinstance(instructions[index], _BeforeInstruction)
                and cast(_BeforeInstruction, instructions[index]).sync
            ):
                current = cast(_BeforeInstruction, instructions[index])
                remap[index] = new_position
                pairs.append(
                    (cast(SyncBeforeHook, current.hook), current.failure_target)
                )
                index += 1
            grouped.append(_FusedBeforeInstruction(tuple(pairs)))
        else:
            remap[index] = len(grouped)
            grouped.append(instruction)
            index += 1
    remap[count] = len(grouped)  # the "no after entered" final position

    result: list[_Instruction] = []
    for instruction in grouped:
        if isinstance(instruction, _FusedBeforeInstruction):
            result.append(
                _FusedBeforeInstruction(
                    tuple((hook, remap[target]) for hook, target in instruction.pairs)
                )
            )
        elif isinstance(instruction, _BeforeInstruction):
            result.append(
                _BeforeInstruction(
                    instruction.hook, remap[instruction.failure_target], instruction.sync
                )
            )
        else:
            result.append(instruction)
    return result


def _compile_tape(endpoint: CallNext, middleware: tuple[Middleware, ...]) -> MiddlewareTape:
    # A synchronous before_sync hook is the fusable form; otherwise the async
    # before hook is used. Sync egress has the same precedence over async
    # egress. Both choices are fixed here, never rediscovered per request.
    hooks: list[
        tuple[
            BeforeHook | SyncBeforeHook | None,
            bool,
            AfterHook | SyncAfterHook | InPlaceAfterHook | None,
            bool,
            bool,
        ]
    ] = []
    for current in middleware:
        before_sync = cast(SyncBeforeHook | None, getattr(current, "before_sync", None))
        after_inplace = cast(
            InPlaceAfterHook | None, getattr(current, "after_inplace", None)
        )
        after_sync = cast(SyncAfterHook | None, getattr(current, "after_sync", None))
        after = (
            after_inplace
            if after_inplace is not None
            else (
                after_sync
                if after_sync is not None
                else cast(AfterHook | None, getattr(current, "after", None))
            )
        )
        if before_sync is not None:
            hooks.append(
                (
                    before_sync,
                    True,
                    after,
                    after_sync is not None,
                    after_inplace is not None,
                )
            )
        else:
            hooks.append(
                (
                    cast(BeforeHook | None, getattr(current, "before", None)),
                    False,
                    after,
                    after_sync is not None,
                    after_inplace is not None,
                )
            )

    before_entries = [
        (index, hook, sync)
        for index, (hook, sync, _, _, _) in enumerate(hooks)
        if hook
    ]
    after_entries = [
        (index, after, sync, inplace)
        for index, (_, _, after, sync, inplace) in reversed(tuple(enumerate(hooks)))
        if after
    ]
    endpoint_position = len(before_entries)
    after_positions = {
        middleware_index: endpoint_position + 1 + offset
        for offset, (middleware_index, _, _, _) in enumerate(after_entries)
    }
    final_position = endpoint_position + 1 + len(after_entries)

    instructions: list[_Instruction] = []
    for middleware_index, hook, sync in before_entries:
        failure_target = min(
            (
                position
                for index, position in after_positions.items()
                if index <= middleware_index
            ),
            default=final_position,
        )
        instructions.append(_BeforeInstruction(cast(BeforeHook, hook), failure_target, sync))
    instructions.append(_EndpointInstruction(endpoint))
    instructions.extend(
        _InPlaceAfterInstruction(cast(InPlaceAfterHook, after))
        if inplace
        else (
            _SyncAfterInstruction(cast(SyncAfterHook, after))
            if sync
            else _AfterInstruction(cast(AfterHook, after))
        )
        for _, after, sync, inplace in after_entries
    )
    return MiddlewareTape(tuple(_fuse_sync_befores(instructions)))


def _adapt_fused(middleware: Middleware) -> LegacyMiddleware:
    before_sync = cast(SyncBeforeHook | None, getattr(middleware, "before_sync", None))
    before = cast(BeforeHook | None, getattr(middleware, "before", None))
    after_inplace = cast(
        InPlaceAfterHook | None, getattr(middleware, "after_inplace", None)
    )
    after_sync = cast(SyncAfterHook | None, getattr(middleware, "after_sync", None))
    after = cast(AfterHook | None, getattr(middleware, "after", None))

    async def adapted(request: Request, call_next: CallNext) -> ResponseValue:
        if before_sync is not None:
            response = before_sync(request)
        else:
            response = None if before is None else await before(request)
        if response is None:
            response = await call_next(request)
        if after_inplace is not None:
            after_inplace(request, response)
        elif after_sync is not None:
            response = after_sync(request, response)
        elif after is not None:
            response = await after(request, response)
        return response

    return adapted


def compile_middleware(
    endpoint: CallNext,
    middleware: Iterable[Middleware],
    *,
    route: MiddlewareRoute | None = None,
) -> CallNext:
    """Compile applicable middleware once, using a tape when every item is fusible.

    Called at startup, once per route and method. Each middleware's `applies_to`
    predicate is evaluated here and never again -- a middleware that does not
    apply is simply not part of what this returns, so it costs the request
    nothing. A middleware with no `applies_to`, or a call with no `route`,
    always applies.

    When every applicable middleware is in the hook form the result is a
    `MiddlewareTape`. One legacy `(request, call_next)` middleware among them
    drops the whole chain to nested closures, with the hook middleware wrapped
    into the legacy shape; the observable behaviour is the same, the flat
    dispatch is not. With nothing applicable, `endpoint` is returned unchanged,
    so an unused registration adds no frame at all.

    Args:
        endpoint: The route handler the compiled chain terminates in.
        middleware: Registered middleware, outermost first.
        route: Static route facts for `applies_to`. None applies everything.

    Returns:
        An awaitable taking the request and returning the response.

    Raises:
        TypeError: A middleware defines both `__call__` and hook attributes.
    """
    applicable = tuple(
        current
        for current in middleware
        if route is None
        or (predicate := getattr(current, "applies_to", None)) is None
        or predicate(route)
    )
    if not applicable:
        return endpoint
    if all(_is_fused(current) for current in applicable):
        return _compile_tape(endpoint, applicable)

    compiled = endpoint
    for current in reversed(applicable):
        next_handler = compiled
        legacy = _adapt_fused(current) if _is_fused(current) else cast(LegacyMiddleware, current)

        async def bound(
            request: Request,
            _middleware: LegacyMiddleware = legacy,
            _next: CallNext = next_handler,
        ) -> ResponseValue:
            return await _middleware(request, _next)

        compiled = bound
    return compiled
