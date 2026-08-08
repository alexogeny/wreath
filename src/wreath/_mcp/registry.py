"""Declared tools, and the argument validation tools and prompts share.

A tool's schema is derived once, when it is declared, rather than per call; the
`tools/list` bytes are cached by `Catalog`, which every declared collection here
shares. That is the same trade the router makes -- compile at startup, spend
nothing at request time -- and it is the only optimization `wreath.mcp` makes,
deliberately: everything below the envelope is already native.

A tool carries an `AuthRequirement` rather than a bare Cedar policy, which is
what lets `expose_routes` hand a route's own access rules through unchanged
instead of translating them into something weaker.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .._auth.requirements import AuthRequirement, PolicyRequirement, merge_requirements
from ..binding import BindingSpec, ValidationError, validate
from ..policy.ratelimit import MemoryRateLimitStore
from .catalog import Catalog
from .limits import ToolRateLimit
from .schema import ToolSignatureError, derive_input_schema

#: What a tool declares when it is gated on nothing at all. Shared rather than
#: rebuilt per tool: it is frozen, and an identity comparison is what the
#: dispatch fast path wants.
NO_REQUIREMENT = AuthRequirement()


def policy_requirement(
    action: str, resource: object | Callable[[Any], object]
) -> AuthRequirement:
    """The requirement a declared `action=` produces.

    `authenticated=True` comes with it, exactly as `@authorize(...)` on a route
    sets it: a Cedar policy is written about a principal, and admitting a call
    with no principal so the engine can deny it is a slower way of arriving at
    the same answer with a worse message.
    """
    return AuthRequirement(
        authenticated=True, policies=(PolicyRequirement(action, resource),)
    )


def bind_arguments(
    spec: BindingSpec | None, arguments: Mapping[str, Any], *, label: str = "arguments"
) -> dict[str, Any]:
    """Validate a call's arguments against a compiled signature.

    Shared by tools and prompts, which differ in what they do with the result
    and not at all in how they check it. Every failure the object can express is
    collected before raising, so a caller that got three arguments wrong learns
    all three at once rather than one per round trip -- the same choice
    `wreath.binding` makes for an HTTP request body.

    Raises:
        ValidationError: One or more arguments are missing, unknown, or of the
            wrong shape.
    """
    errors: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    known: set[str] = set()

    if spec is not None:
        for parameter, wire_name, annotation, default in spec.query_params:
            known.add(wire_name)
            if wire_name not in arguments:
                if default is inspect.Parameter.empty:
                    errors.append(
                        {"loc": [label, wire_name], "msg": "field required", "type": "missing"}
                    )
                continue
            try:
                kwargs[parameter] = validate(
                    annotation, arguments[wire_name], (label, wire_name)
                )
            except ValidationError as error:
                errors.extend(error.errors)
        if spec.body is not None:
            parameter, annotation = spec.body
            known.add(parameter)
            if parameter not in arguments:
                errors.append(
                    {"loc": [label, parameter], "msg": "field required", "type": "missing"}
                )
            else:
                try:
                    kwargs[parameter] = validate(
                        annotation, arguments[parameter], (label, parameter)
                    )
                except ValidationError as error:
                    errors.extend(error.errors)

    for supplied in arguments:
        if supplied not in known:
            # The schema says `additionalProperties: false`, so accepting an
            # argument the callable never declared would make the published
            # schema a lie. Name it instead: a model that hallucinated an
            # argument can only correct itself if it is told which one.
            errors.append(
                {"loc": [label, supplied], "msg": "unexpected argument", "type": "unexpected"}
            )

    if errors:
        raise ValidationError(errors)
    return kwargs


@dataclass(frozen=True, slots=True)
class Tool:
    """One declared tool: what `tools/list` renders and `tools/call` invokes.

    A tool is not a route, but its handler obeys the route contract -- the first
    parameter is the `Request`, every later parameter is bound by name -- so the
    same signature works in both places and the schema derivation is shared.

    Attributes:
        name: The name a client calls. Unique within one `MCP`.
        description: What the tool does, and when a model should reach for it.
            Never empty: a tool without a description is a tool that gets
            misused, and registration refuses one.
        handler: The async callable, invoked as `handler(request, **arguments)`.
        input_schema: JSON Schema for the `arguments` object.
        title: An optional human-facing display name.
        binding_spec: The compiled signature, or None for a handler that takes
            only the request.
        requirement: What a caller must satisfy to invoke it -- exactly the
            `AuthRequirement` a route carries, so the authorizer cannot tell an
            MCP call from an HTTP one and `expose_routes` can hand a route's own
            requirement straight through rather than translating it into
            something weaker. `NO_REQUIREMENT` for a tool anyone who reached the
            endpoint may call.
        rate_limit: The declared per-caller ceiling, or None.
        limiter: The token bucket enforcing `rate_limit`. One per tool, because
            two tools sharing a bucket would spend each other's allowance.
        route: The path of the route this tool was derived from, or None for a
            tool declared with `@mcp.tool`. Carried so a refusal can name the
            route rather than only the tool it would have become.
        sampling_requirement: What a caller must satisfy for this tool to ask
            the client's model to generate, or **None when the tool did not
            declare `sampling=` at all** -- which is the default, and means it
            may not sample. Whether a given tool may spend the caller's model on
            text of its own choosing is exactly a policy question, so it is one
            more `AuthRequirement` decided by the same authorizer as everything
            else here; `sampling=True` declares the capability with no policy
            attached, which is the shape a development server wants.
        elicitation_requirement: The same, for putting a form in front of the
            person at the other end, and **None by default for the same reason**.
            An elicitation renders inside a client UI the user already trusts, so
            a tool that asks for `{"api_key": str}` is a phishing surface wearing
            the client's chrome; "the user can decline" is precisely the control
            social engineering defeats. Which tools may prompt a human is
            therefore a deployment's decision, decided by the same authorizer.
    """

    name: str
    description: str
    handler: Callable[..., Any]
    input_schema: dict[str, Any]
    title: str | None = None
    binding_spec: BindingSpec | None = None
    requirement: AuthRequirement = NO_REQUIREMENT
    rate_limit: ToolRateLimit | None = None
    limiter: MemoryRateLimitStore | None = None
    route: str | None = None
    sampling_requirement: AuthRequirement | None = None
    elicitation_requirement: AuthRequirement | None = None

    def manifest(self) -> dict[str, Any]:
        """The `tools/list` entry for this tool."""
        entry: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title is not None:
            entry["title"] = self.title
        return entry

    def bind(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Validate `arguments` against the signature and return call kwargs.

        Raises:
            ValidationError: One or more arguments are missing, unknown, or of
                the wrong shape.
        """
        return bind_arguments(self.binding_spec, arguments)


class ToolRegistry(Catalog):
    """The declared tools of one `MCP`, plus the cached `tools/list` bytes."""

    __slots__ = ()

    noun = "tool"
    ceiling = "max_tools"
    listing_key = "tools"

    def __init__(self, *, max_tools: int = 256) -> None:
        super().__init__(max_tools)

    @property
    def tools(self) -> dict[str, Tool]:
        return self.entries

    def add(self, tool: Tool) -> None:
        """Register `tool`, refusing a duplicate name or a full registry.

        Refusing at registration means an over-full server fails at import
        rather than at call time: a `tools/list` a model cannot hold in its
        context is a design problem, and the design is being written right here.

        Raises:
            ValueError: A tool of that name is already registered, or the
                server is already at `MCPLimits.max_tools`.
        """
        self.insert(tool.name, tool)



def actions_by_type(entries: Iterable[Any]) -> dict[str, tuple[str, ...]]:
    """Resource type -> the Cedar actions `entries` are gated on.

    The same shape `wreath.authorization.declared_actions` returns for an
    application's routes, and read off the declarations the same way -- from
    what is enforced, so there is no second list to keep in step. An action
    without the conventional `Type::verb` separator is grouped under `""`.
    """
    found: dict[str, set[str]] = {}
    for entry in entries:
        # A tool's outbound gates are as reachable by a model as the tool itself,
        # so they belong in the one place a deployment reads to see everything a
        # model can reach. Leaving them out would make the document quietly
        # incomplete for exactly the tools that need reviewing most -- and the
        # elicitation gate above all, since that is the one that ends in a prompt
        # in front of a person.
        requirements = (
            entry.requirement,
            getattr(entry, "sampling_requirement", None),
            getattr(entry, "elicitation_requirement", None),
        )
        for requirement in requirements:
            if requirement is None:
                continue
            for policy in requirement.policies:
                resource_type, separator, _verb = policy.action.partition("::")
                found.setdefault(resource_type if separator else "", set()).add(policy.action)
    return {name: tuple(sorted(actions)) for name, actions in sorted(found.items())}


def build_tool(
    handler: Callable[..., Any],
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    action: str | None = None,
    resource: object | Callable[[Any], object] = None,
    rate_limit: ToolRateLimit | None = None,
    requirement: AuthRequirement | None = None,
    second_factor: float | None = None,
    route: str | None = None,
    sampling: str | bool | None = None,
    elicitation: str | bool | None = None,
) -> Tool:
    """Compile one handler into a `Tool`, deriving its schema.

    `requirement=` is how `expose_routes` hands a route's own access rules
    through unchanged; it merges with anything `action=` or `second_factor=`
    declares here, and merging can only ever add.

    Raises:
        TypeError: The handler is not an async function.
        ValueError: No description was given and the handler has no docstring,
            `resource=` was given without `action=`, or `second_factor=` is not
            positive.
        ToolSignatureError: The signature binds from a source a call cannot fill.
    """
    tool_name = name or getattr(handler, "__name__", "")
    if not tool_name:
        # A `functools.partial`, a callable instance, a lambda assigned nowhere:
        # all legitimate handlers, none of which can name themselves.
        raise ValueError(
            "a tool declared from a callable without a `__name__` must be given "
            "one: `mcp.tool(handler, name=...)`."
        )
    if not inspect.iscoroutinefunction(handler):
        raise TypeError(
            f"tool {tool_name!r} must be an async function. Wreath invokes a "
            "tool the way it invokes a route handler, and a synchronous "
            "callable would block the event loop for every other session."
        )
    text = description if description is not None else inspect.getdoc(handler)
    if not text:
        subject = (
            f"tool {tool_name!r}" if route is None else f"route {route} ({tool_name!r})"
        )
        how = (
            "Pass `description=` or give the handler a docstring"
            if route is None
            else "Give its handler a docstring -- the same text the OpenAPI "
            "document already uses as the operation description"
        )
        raise ValueError(
            f"{subject} needs a description. {how}: the description is the "
            "entire basis on which a model decides whether to call it, and an "
            "undescribed tool is one that gets called wrongly."
        )
    if action is None and resource is not None:
        raise ValueError(
            f"tool {tool_name!r} was given a `resource=` with no `action=`. A "
            "Cedar decision needs both, and a resource on its own gates nothing."
        )
    if second_factor is not None and second_factor <= 0:
        # The same refusal `@second_factor` makes, for the same reason: a window
        # of zero or less can never be satisfied, so it is a typo rather than a
        # policy of never allowing the tool -- which is what `deny` is for.
        raise ValueError(
            f"tool {tool_name!r} was given `second_factor={second_factor!r}`. The "
            "window is in seconds and must be positive; one that is zero or less "
            "can never be satisfied by any caller."
        )
    declared = NO_REQUIREMENT if action is None else policy_requirement(action, resource)
    if second_factor is not None:
        # Before the merge below, so a route that already carried a *shorter*
        # window keeps it: `merge_requirements` takes the minimum, and a tool
        # declaration must not be able to relax what the route asked for.
        declared = replace(declared, authenticated=True, second_factor=second_factor)
    if requirement is not None and requirement is not NO_REQUIREMENT:
        declared = merge_requirements(requirement, declared)
    limiter: MemoryRateLimitStore | None = None
    if rate_limit is not None:
        # Configured once, here, because `configure` refuses a second call --
        # which is what keeps one tool's ceiling from being silently rewritten
        # by the next tool's registration.
        limiter = MemoryRateLimitStore()
        limiter.configure(rate_limit.capacity, rate_limit.rate)
    input_schema, spec = derive_input_schema(handler, tool_name, route=route)
    return Tool(
        name=tool_name,
        description=text,
        handler=handler,
        input_schema=input_schema,
        title=title,
        binding_spec=spec,
        requirement=declared,
        rate_limit=rate_limit,
        limiter=limiter,
        route=route,
        sampling_requirement=gate_requirement(
            tool_name,
            sampling,
            keyword="sampling",
            purpose="ask the client's model to generate",
        ),
        elicitation_requirement=gate_requirement(
            tool_name,
            elicitation,
            keyword="elicitation",
            purpose="put a form in front of the person at the other end",
        ),
    )


def gate_requirement(
    tool_name: str, declared: str | bool | None, *, keyword: str, purpose: str
) -> AuthRequirement | None:
    """What one outbound gate declares, or None when it declared nothing.

    `sampling=` and `elicitation=` are the same declaration about two different
    things a tool may do to the caller rather than for it, so they are one
    function: a second copy would be a second place for the bool/action/None
    handling to drift, and the two must stay symmetric because the reason for
    gating either is the same reason.

    The Cedar resource is the tool's own name, exactly as a prompt's is: a tool
    that may sample, or may prompt, is a stable, named thing a policy can be
    written about, and resolving a second identifier from the call's arguments
    would make the decision depend on what the model happened to ask for.

    Raises:
        ValueError: The keyword was given something that is neither an action
            nor a bool.
    """
    if declared is None or declared is False:
        return None
    if declared is True:
        return NO_REQUIREMENT
    if not isinstance(declared, str) or not declared:
        raise ValueError(
            f"tool {tool_name!r} was given `{keyword}={declared!r}`. Pass the "
            f"Cedar action a caller must be allowed for this tool to {purpose}, "
            "or `True` to declare the capability with no policy attached."
        )
    return policy_requirement(declared, tool_name)


__all__ = [
    "NO_REQUIREMENT",
    "Tool",
    "ToolRateLimit",
    "ToolRegistry",
    "ToolSignatureError",
    "actions_by_type",
    "bind_arguments",
    "build_tool",
    "gate_requirement",
    "policy_requirement",
]
