"""Request/response middleware contracts and startup compilation."""

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
type RoutePredicate = Callable[[MiddlewareRoute], bool]


@dataclass(frozen=True, slots=True)
class MiddlewareRoute:
    """Static route facts available while middleware is compiled."""

    path: str
    method: str
    endpoint: CallNext
    authenticated: bool = False


class LegacyMiddleware(Protocol):
    async def __call__(self, request: Request, call_next: CallNext) -> ResponseValue: ...


@dataclass(frozen=True, slots=True)
class MiddlewareHooks:
    """Middleware hooks that can be fused into a linear execution tape.

    A before hook returns ``None`` to continue or a response value to
    short-circuit. After hooks receive responses from either path.

    ``before_sync`` is the synchronous form of ``before``: when set, the hook
    is a plain function and a contiguous run of such hooks executes in one
    synchronous pass with no per-hook coroutine or await. Provide ``before``
    (async) or ``before_sync`` (sync), not both.
    """

    before: BeforeHook | None = None
    after: AfterHook | None = None
    applies_to: RoutePredicate | None = None
    before_sync: SyncBeforeHook | None = None


@dataclass(frozen=True, slots=True)
class PipelineHooks:
    """Global hooks at explicit request-pipeline cost boundaries.

    Each stage hook returns ``None`` to continue or a response to terminate.
    ``before`` is ingress, while ``after`` finalizes every entered response.
    """

    before: BeforeHook | None = None
    miss: BeforeHook | None = None
    pre_auth: BeforeHook | None = None
    identity: BeforeHook | None = None
    action: BeforeHook | None = None
    after: AfterHook | None = None

    @property
    def global_scope(self) -> bool:
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
    ``(hook, failure_target)`` pair short-circuits to its own target -- the
    after-region position of its middleware -- so entered after hooks still
    run correctly when a fused hook responds."""

    pairs: tuple[tuple[SyncBeforeHook, int], ...]


@dataclass(frozen=True, slots=True)
class _EndpointInstruction:
    endpoint: CallNext


@dataclass(frozen=True, slots=True)
class _AfterInstruction:
    hook: AfterHook


type _Instruction = (
    _BeforeInstruction | _FusedBeforeInstruction | _EndpointInstruction | _AfterInstruction
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

_OPCODES: tuple[tuple[type, int, str], ...] = (
    (_FusedBeforeInstruction, _OP_FUSED_BEFORE, "fused_before"),
    (_BeforeInstruction, _OP_BEFORE, "before"),
    (_EndpointInstruction, _OP_ENDPOINT, "endpoint"),
    (_AfterInstruction, _OP_AFTER, "after"),
)


def _opcode(instruction: _Instruction) -> tuple[int, str]:
    for kind, code, name in _OPCODES:
        if isinstance(instruction, kind):
            return code, name
    raise TypeError(f"unknown middleware instruction: {instruction!r}")


class MiddlewareTape:
    """One immutable, linear middleware program compiled for a route."""

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
            else:
                response = await instruction.hook(request, response)
                position += 1
        if response is _UNSET:
            raise RuntimeError("middleware tape completed without a response")
        return response


_HOOK_ATTRIBUTES = ("before", "before_sync", "after")


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
    ``_FusedBeforeInstruction`` and remap every jump position to the new layout.

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
    # before hook is used. (name, hook, is_sync, after) per middleware.
    hooks: list[tuple[BeforeHook | SyncBeforeHook | None, bool, AfterHook | None]] = []
    for current in middleware:
        before_sync = cast(SyncBeforeHook | None, getattr(current, "before_sync", None))
        after = cast(AfterHook | None, getattr(current, "after", None))
        if before_sync is not None:
            hooks.append((before_sync, True, after))
        else:
            hooks.append((cast(BeforeHook | None, getattr(current, "before", None)), False, after))

    before_entries = [
        (index, hook, sync) for index, (hook, sync, _) in enumerate(hooks) if hook
    ]
    after_entries = [
        (index, after)
        for index, (_, _, after) in reversed(tuple(enumerate(hooks)))
        if after
    ]
    endpoint_position = len(before_entries)
    after_positions = {
        middleware_index: endpoint_position + 1 + offset
        for offset, (middleware_index, _) in enumerate(after_entries)
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
    instructions.extend(_AfterInstruction(after) for _, after in after_entries)
    return MiddlewareTape(tuple(_fuse_sync_befores(instructions)))


def _adapt_fused(middleware: Middleware) -> LegacyMiddleware:
    before_sync = cast(SyncBeforeHook | None, getattr(middleware, "before_sync", None))
    before = cast(BeforeHook | None, getattr(middleware, "before", None))
    after = cast(AfterHook | None, getattr(middleware, "after", None))

    async def adapted(request: Request, call_next: CallNext) -> ResponseValue:
        if before_sync is not None:
            response = before_sync(request)
        else:
            response = None if before is None else await before(request)
        if response is None:
            response = await call_next(request)
        if after is not None:
            response = await after(request, response)
        return response

    return adapted


def compile_middleware(
    endpoint: CallNext,
    middleware: Iterable[Middleware],
    *,
    route: MiddlewareRoute | None = None,
) -> CallNext:
    """Compile applicable middleware once, using a tape when every item is fusible."""
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
