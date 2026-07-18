"""Request/response middleware contracts and startup compilation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ..request import Request

type ResponseValue = Any
type CallNext = Callable[[Request], Awaitable[ResponseValue]]
type BeforeHook = Callable[[Request], Awaitable[ResponseValue | None]]
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
    """

    before: BeforeHook | None = None
    after: AfterHook | None = None
    applies_to: RoutePredicate | None = None


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


@dataclass(frozen=True, slots=True)
class _EndpointInstruction:
    endpoint: CallNext


@dataclass(frozen=True, slots=True)
class _AfterInstruction:
    hook: AfterHook


type _Instruction = _BeforeInstruction | _EndpointInstruction | _AfterInstruction
_UNSET = object()


class MiddlewareTape:
    """One immutable, linear middleware program compiled for a route."""

    __slots__ = ("_instructions", "operations")

    def __init__(self, instructions: tuple[_Instruction, ...]) -> None:
        self._instructions = instructions
        self.operations = tuple(
            "before"
            if isinstance(instruction, _BeforeInstruction)
            else "endpoint"
            if isinstance(instruction, _EndpointInstruction)
            else "after"
            for instruction in instructions
        )

    async def __call__(self, request: Request) -> ResponseValue:
        instructions = self._instructions
        response: ResponseValue = _UNSET
        position = 0
        while position < len(instructions):
            instruction = instructions[position]
            if isinstance(instruction, _BeforeInstruction):
                candidate = await instruction.hook(request)
                if candidate is None:
                    position += 1
                else:
                    response = candidate
                    position = instruction.failure_target
            elif isinstance(instruction, _EndpointInstruction):
                response = await instruction.endpoint(request)
                position += 1
            else:
                response = await instruction.hook(request, response)
                position += 1
        if response is _UNSET:
            raise RuntimeError("middleware tape completed without a response")
        return response


def _is_fused(middleware: Middleware) -> bool:
    return isinstance(middleware, MiddlewareHooks) or any(
        hasattr(middleware, attribute) for attribute in ("before", "after")
    )


def _compile_tape(endpoint: CallNext, middleware: tuple[Middleware, ...]) -> MiddlewareTape:
    hooks = [
        (
            cast(BeforeHook | None, getattr(current, "before", None)),
            cast(AfterHook | None, getattr(current, "after", None)),
        )
        for current in middleware
    ]
    before_entries = [(index, before) for index, (before, _) in enumerate(hooks) if before]
    after_entries = [
        (index, after)
        for index, (_, after) in reversed(tuple(enumerate(hooks)))
        if after
    ]
    endpoint_position = len(before_entries)
    after_positions = {
        middleware_index: endpoint_position + 1 + offset
        for offset, (middleware_index, _) in enumerate(after_entries)
    }
    final_position = endpoint_position + 1 + len(after_entries)

    instructions: list[_Instruction] = []
    for middleware_index, before in before_entries:
        failure_target = min(
            (
                position
                for index, position in after_positions.items()
                if index <= middleware_index
            ),
            default=final_position,
        )
        instructions.append(_BeforeInstruction(before, failure_target))
    instructions.append(_EndpointInstruction(endpoint))
    instructions.extend(_AfterInstruction(after) for _, after in after_entries)
    return MiddlewareTape(tuple(instructions))


def _adapt_fused(middleware: Middleware) -> LegacyMiddleware:
    before = cast(BeforeHook | None, getattr(middleware, "before", None))
    after = cast(AfterHook | None, getattr(middleware, "after", None))

    async def adapted(request: Request, call_next: CallNext) -> ResponseValue:
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
