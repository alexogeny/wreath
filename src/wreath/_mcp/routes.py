"""Turning routes you already have into tools -- only the ones you name.

`fastapi-mcp` exposes every route by default. That is the wrong default for a
framework that ships an authorizer, because it converts an application's entire
HTTP surface into model-callable actions in one line, including the destructive
ones, and the person running that line usually has one endpoint in mind. So this
adapter takes an **explicit selector** and has no `all=True`. If you find
yourself wanting one, the thing you want is a tag.

Two refusals follow from the same reasoning, and both happen at registration:

- **A route with no description is refused.** The description is the entire
  basis on which a model decides whether this is the tool for the job, and a
  route's description is its handler's docstring -- the same text the OpenAPI
  document carries. An undescribed tool is not ignored; it is guessed at.
- **A route whose signature an MCP call cannot fill is refused, by name.** A
  `tools/call` carries one JSON object of arguments and nothing else, so a path
  placeholder, a header, a cookie, an upload or a `Depends(...)` is a parameter
  no caller can supply. Route handlers hit this far more often than declared
  tools do, so the message names the route as well as the parameter.
- **A route carrying its own middleware or dependencies is refused.** A tool
  invokes the handler; it does not replay the route's chain, and there is no
  request through that chain for it to replay. So a `before` hook that refuses,
  or a `dependencies=[Depends(require_api_key)]` that raises, would simply not
  run -- and those are the shapes a guard is usually written in. Carrying the
  `AuthRequirement` and dropping the two controls beside it silently is the
  worst of the three available answers; refusing is the rule here, and it leaves
  the door open to running them later where admitting the call would not.

What a route brings with it is the point. The exposed tool carries the route's
own `AuthRequirement` -- whatever `@authenticated`, `@roles`, `@permissions`,
`@authorize` and any router it was included from imposed -- so a route that was
behind a Cedar policy is still behind it when a model calls it. Exposing a route
must not be a way around the controls that were put in front of it, and the
identical dispatch runs it: the same rate limiting, the same Flight Recorder
marker, the same five outcomes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .._auth.requirements import merge_requirements, requirement_for
from .limits import ToolRateLimit
from .registry import Tool, build_tool


def _definitions(source: Any) -> tuple[Any, ...]:
    """Every flattened route definition on an application or a router."""
    image = getattr(source, "_application_image", None)
    if image is not None:
        return tuple(image.routes())
    routes = getattr(source, "routes", None)
    if routes is not None:
        return tuple(routes)
    raise TypeError(
        "expose_routes needs a Wreath application or a Router to read routes "
        f"from; {type(source).__name__} has neither."
    )


def expose_routes(
    mcp: Any,
    app: Any = None,
    *,
    tags: Iterable[str] = (),
    include: Iterable[str] = (),
    predicate: Callable[[Any], bool] | None = None,
    prefix: str = "",
    rate_limit: ToolRateLimit | None = None,
) -> tuple[Tool, ...]:
    """Declare selected existing routes as MCP tools.

    Call it **after** the routes exist: an application is read here, not
    retained, so a route registered afterwards is not picked up.

        expose_routes(mcp, app, tags=("sightings",))

    A route is selected when it matches **any** selector given, and at least one
    selector must be given -- there is deliberately no way to say "all of them".
    A selector that matches nothing raises rather than exposing nothing quietly,
    because a tag that no longer exists is a typo far more often than it is an
    intention.

    The tool's name is the route's `operation_id` when it has one and the
    handler's name otherwise, its description is the handler's docstring, and
    its `inputSchema` is derived from the signature by the same code that
    derives a declared tool's. One tool per route, not per method: a tool
    invokes the handler directly, so the HTTP verb the route answers on is not
    part of the call.

    Args:
        mcp: The `MCP` server to declare the tools on.
        app: The application or `Router` to read routes from. Defaults to the
            one `mcp` is mounted on.
        tags: Expose every route carrying any of these tags.
        include: Expose every route whose path is named here, exactly.
        predicate: Expose every route this returns true for. Receives the
            `RouteDefinition`, so `lambda route: "GET" in route.methods` is how
            you keep the mutating half of a tag out.
        prefix: Prepended to each derived tool name, for a server that exposes
            two applications and would otherwise collide on `list_items`.
        rate_limit: A `ToolRateLimit` applied to every tool this call declares.
            Route-derived tools are the ones most likely to want it: a route was
            written for a person clicking, and a model does not click.

    Returns:
        The tools that were declared, in the order the routes were registered.

    Raises:
        ValueError: No selector was given, no route matched, a selected route
            has no docstring, a selected route carries route middleware or route
            dependencies (see `_check_carryable`), or a derived name collides
            with a declared tool.
        ToolSignatureError: A selected route binds a parameter an MCP call
            cannot supply. The message names the route and the parameter.
    """
    wanted_tags = frozenset(tags)
    wanted_paths = frozenset(include)
    if not wanted_tags and not wanted_paths and predicate is None:
        raise ValueError(
            "expose_routes needs a selector: `tags=`, `include=`, or a "
            "`predicate=`. There is no flag that exposes everything, on "
            "purpose -- an application's whole HTTP surface turned into "
            "model-callable actions in one line includes the destructive half, "
            "and nobody reviews a line that short."
        )
    source = app if app is not None else getattr(mcp, "_app", None)
    if source is None:
        raise ValueError(
            "expose_routes has nothing to read routes from: pass the "
            "application, or mount the MCP server on one first."
        )

    selected = [
        definition
        for definition in _definitions(source)
        # complexity: allow SL-LINEAR-METHOD -- tag filter compares both sets
        if (wanted_tags and wanted_tags.intersection(definition.tags))
        or definition.path in wanted_paths
        or (predicate is not None and predicate(definition))
    ]
    if not selected:
        raise ValueError(
            "expose_routes matched no route. A selector that selects nothing is "
            "a typo far more often than it is an intention, so this is an error "
            "rather than a server with no tools on it."
        )

    declared: list[Tool] = []
    image = getattr(source, "_application_image", None)
    effective = (
        {
            id(definition): requirement
            for definition, requirement in zip(image.routes(), image.requirements(), strict=True)
        }
        if image is not None
        else {}
    )
    for definition in selected:
        _check_carryable(definition)
        tool = build_tool(
            definition.endpoint,
            name=prefix + _tool_name(definition),
            description=None,
            rate_limit=rate_limit,
            # Whatever the route was behind, the tool is behind. Merging the
            # endpoint's own decorators with the definition's inherited ones is
            # exactly what the application does before it enforces them, so the
            # two cannot come to different conclusions.
            requirement=effective.get(id(definition))
            or merge_requirements(definition.requirement, requirement_for(definition.endpoint)),
            route=definition.path,
        )
        mcp._registry.add(tool)
        declared.append(tool)
    return tuple(declared)


def _check_carryable(definition: Any) -> None:
    """Refuse a route whose controls a tool cannot carry.

    `definition.requirement` is carried; `definition.middleware` and
    `definition.dependencies` cannot be. A tool calls the handler, and the route
    chain that would have run those does not exist on an MCP call -- so exposing
    such a route would publish it to a model with strictly less in front of it
    than the HTTP path has, which is the one thing this adapter's docstring
    promises it will not do.

    Both are route- and router-scoped only. Application middleware installed
    with `app.add_middleware(...)` is not in `definition.middleware`, and it
    covers the MCP endpoint already -- an MCP call is a route activation like
    any other -- so nothing here refuses a route for a control it still has.

    Raises:
        ValueError: The route carries route middleware or route dependencies.
    """
    for attribute, spelling, what in (
        ("middleware", "middleware=", "a `before` hook that returns a response"),
        ("dependencies", "dependencies=", "a `Depends(...)` that raises"),
    ):
        carried = getattr(definition, attribute, ())
        if not carried:
            continue
        raise ValueError(
            f"route {definition.path} carries route {attribute} and cannot be "
            f"exposed as a tool. A tool invokes the handler; it does not replay "
            f"the route's chain, so {what} in its {spelling} would not run and "
            f"the tool would be reachable with less in front of it than the "
            f"route is. Move the check into the handler, express it as "
            f"`@authenticated`/`@roles`/`@permissions`/`@authorize` -- which a "
            f"tool does carry -- or narrow the selector so this route is not "
            f"chosen and declare a tool of your own that calls the same code."
        )


def _tool_name(definition: Any) -> str:
    name = definition.operation_id or getattr(definition.endpoint, "__name__", "")
    if not name:
        raise ValueError(
            f"route {definition.path} is served by a callable with no "
            "`__name__` and no `operation_id=`, so there is no name to address "
            "it by. Give the route an `operation_id`."
        )
    return name


__all__ = ["expose_routes"]
