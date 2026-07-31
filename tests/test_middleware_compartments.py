"""Per-route middleware compartments: what a route declines, and what it cannot.

A global middleware may expose `applies_to(method, path)`, consulted once per
route at compile time. Routes it declines dispatch through a program compiled
without it, so declining costs nothing per request -- there is no gate to
evaluate, the hook is simply absent from the tape that route runs.

That reordering is the whole risk. `_handle_http` runs the global tape *before*
routing, deliberately: `ProxyHeadersMiddleware` rewrites a forwarded Host that
host-routing then matches on, and a rate limiter is documented to count a flood
of 404s. `_handle_http_compartment` routes first, so every request shape where
that would change an answer has to fall back -- and the tests that matter here
are the fallbacks, not the fast path.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Response, Wreath
from wreath._auth.backends import BearerTokenBackend
from wreath._auth.decorators import roles
from wreath._auth.models import Identity


async def invoke(
    app: Wreath,
    path: str = "/hot",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers or [],
            "query_string": b"",
        },
        receive,
        send,
    )
    return sent


def status_of(sent: list[dict[str, Any]]) -> int:
    return next(
        message["status"]
        for message in sent
        if message.get("type") in ("http.response.start", "wreath.response")
    )


class Recorder:
    """A global hook pair that names itself in a shared event list."""

    global_scope = True

    def __init__(self, name: str, events: list[str], only: str | None = None) -> None:
        self.name = name
        self.events = events
        self.only = only

    def before_sync(self, request: Any) -> None:
        self.events.append(f"{self.name}-in")
        return None

    def after_inplace(self, request: Any, response: Any) -> None:
        self.events.append(f"{self.name}-out")

    def applies_to(self, method: str, path: str) -> bool:
        return self.only is None or path == self.only


def build(events: list[str], **kwargs: Any) -> Wreath:
    app = Wreath(**kwargs)
    app.add_middleware(Recorder("always", events))
    app.add_middleware(Recorder("cold-only", events, only="/cold"))

    @app.get("/hot")
    async def hot(request: Any) -> Response:
        return Response(b"hot")

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return Response(b"cold")

    app._compile_routes()
    return app


@pytest.mark.asyncio
async def test_a_declined_middleware_does_not_run_on_that_route() -> None:
    events: list[str] = []
    app = build(events)

    assert status_of(await invoke(app, "/hot")) == 200
    assert events == ["always-in", "always-out"]

    events.clear()
    assert status_of(await invoke(app, "/cold")) == 200
    assert events == ["always-in", "cold-only-in", "cold-only-out", "always-out"]


@pytest.mark.asyncio
async def test_declining_nothing_leaves_the_application_unchanged() -> None:
    """No `applies_to` anywhere means no per-route programs and no new branch."""
    events: list[str] = []
    app = Wreath()
    app.add_middleware(Recorder("a", events))

    @app.get("/hot")
    async def hot(request: Any) -> Response:
        return Response(b"hot")

    app._compile_routes()
    assert app._route_programs is None
    assert app._dispatch_http == app._handle_http


@pytest.mark.asyncio
async def test_the_compartment_dispatcher_is_selected_only_when_it_is_safe() -> None:
    events: list[str] = []
    assert build(events)._dispatch_http.__name__ == "_handle_http_compartment"


@pytest.mark.asyncio
async def test_a_host_routed_application_keeps_the_general_dispatcher() -> None:
    """Host routing matches on a Host `ProxyHeadersMiddleware` may rewrite."""
    events: list[str] = []
    app = Wreath()
    app.add_middleware(Recorder("cold-only", events, only="/cold"))

    @app.get("/hot", host="example.com")
    async def hot(request: Any) -> Response:
        return Response(b"hot")

    app._compile_routes()
    assert app._dynamic_matcher is not None
    assert app._dispatch_http == app._handle_http


@pytest.mark.asyncio
async def test_a_miss_still_runs_the_whole_tape() -> None:
    """A rate limiter counts 404s, so a miss must not be compartmentalized."""
    events: list[str] = []
    app = build(events)

    assert status_of(await invoke(app, "/nothing-here")) == 404
    assert events == ["always-in", "cold-only-in", "cold-only-out", "always-out"]


@pytest.mark.asyncio
async def test_an_authenticated_route_falls_back_and_runs_the_whole_tape() -> None:
    """The route behind a ticket is not known until authentication has run."""
    events: list[str] = []

    async def verify(token: str) -> Identity | None:
        return Identity(id="u1", roles=frozenset({"admin"})) if token == "tok" else None

    app = Wreath()
    app.add_middleware(Recorder("always", events))
    app.add_middleware(Recorder("cold-only", events, only="/cold"))
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/hot")
    @roles("admin")
    async def hot(request: Any) -> Response:
        return Response(b"hot")

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return Response(b"cold")

    app._compile_routes()
    sent = await invoke(app, "/hot", headers=[(b"authorization", b"Bearer tok")])
    assert status_of(sent) == 200
    assert events == ["always-in", "cold-only-in", "cold-only-out", "always-out"]


class Refuser:
    """A global middleware that answers instead of the handler."""

    global_scope = True

    def __init__(self, events: list[str], only: str | None = None) -> None:
        self.events = events
        self.only = only

    def before_sync(self, request: Any) -> Any:
        self.events.append("refuser-in")
        return Response(b"refused", status=403)

    def after_inplace(self, request: Any, response: Any) -> None:
        self.events.append("refuser-out")

    def applies_to(self, method: str, path: str) -> bool:
        return self.only is None or path == self.only


@pytest.mark.asyncio
async def test_partial_unwind_is_preserved_inside_a_compartment() -> None:
    """A refusing hook keeps its own egress; hooks after it never ran, so theirs
    must not run either -- with indices from the compartment's own program, not
    the global one."""
    events: list[str] = []
    app = Wreath()
    app.add_middleware(Recorder("first", events))
    app.add_middleware(Recorder("skipped", events, only="/cold"))
    app.add_middleware(Refuser(events))
    app.add_middleware(Recorder("last", events))

    @app.get("/hot")
    async def hot(request: Any) -> Response:
        raise AssertionError("the refusing hook should have answered")

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return Response(b"cold")

    app._compile_routes()
    sent = await invoke(app, "/hot")

    assert status_of(sent) == 403
    # `last` never ran its `before`, so it does not unwind. `refuser` completed
    # its own `before`, so it does. `skipped` is absent from this route.
    assert events == ["first-in", "refuser-in", "refuser-out", "first-out"]


class Exploder:
    global_scope = True

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def before_sync(self, request: Any) -> None:
        raise RuntimeError("hook failed")

    def after_inplace(self, request: Any, response: Any) -> None:
        self.events.append("exploder-out")


@pytest.mark.asyncio
async def test_a_raising_hook_unwinds_only_the_prefix_that_completed() -> None:
    events: list[str] = []
    app = Wreath()
    app.add_middleware(Recorder("first", events))
    app.add_middleware(Recorder("skipped", events, only="/cold"))
    app.add_middleware(Exploder(events))

    @app.get("/hot")
    async def hot(request: Any) -> Response:
        raise AssertionError("unreachable")

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return Response(b"cold")

    app._compile_routes()
    sent = await invoke(app, "/hot")

    assert status_of(sent) == 500
    # The exploder's own `before` did not complete, so its egress is excluded.
    assert events == ["first-in", "first-out"]


@pytest.mark.asyncio
async def test_a_handler_error_still_unwinds_the_compartment() -> None:
    events: list[str] = []
    app = Wreath()
    app.add_middleware(Recorder("always", events))
    app.add_middleware(Recorder("cold-only", events, only="/cold"))

    @app.get("/hot")
    async def hot(request: Any) -> Response:
        raise RuntimeError("handler failed")

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return Response(b"cold")

    app._compile_routes()
    sent = await invoke(app, "/hot")

    assert status_of(sent) == 500
    assert events == ["always-in", "always-out"]


@pytest.mark.asyncio
async def test_path_parameters_are_published_after_the_hooks_as_before() -> None:
    """`_handle_http` routes after the tape, so a hook has never seen these."""
    seen: list[dict[str, str]] = []
    events: list[str] = []

    class Peeker(Recorder):
        def before_sync(self, request: Any) -> None:
            seen.append(dict(request.path_params))
            return None

    app = Wreath()
    app.add_middleware(Peeker("peek", events))
    app.add_middleware(Recorder("cold-only", events, only="/cold"))

    @app.get("/hot/{item}")
    async def hot(request: Any) -> Response:
        return Response(request.path_params["item"].encode())

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return Response(b"cold")

    app._compile_routes()
    sent = await invoke(app, "/hot/42")

    assert status_of(sent) == 200
    assert seen == [{}]
    assert any(message.get("body") == b"42" for message in sent)


@pytest.mark.asyncio
async def test_a_declining_predicate_that_raises_fails_the_compile() -> None:
    """Boot is where loud belongs: swallowing this would silently drop policy."""

    class Broken:
        global_scope = True

        def before_sync(self, request: Any) -> None:
            return None

        def applies_to(self, method: str, path: str) -> bool:
            raise ValueError("cannot decide")

    app = Wreath()
    app.add_middleware(Broken())

    @app.get("/hot")
    async def hot(request: Any) -> Response:
        return Response(b"hot")

    with pytest.raises(ValueError, match="cannot decide"):
        app._compile_routes()


@pytest.mark.asyncio
async def test_compartments_answer_what_a_truncated_stack_answers() -> None:
    """The saving has to reach the same response the short stack would."""
    events: list[str] = []
    compartmented = build(events)
    truncated = Wreath()
    truncated.add_middleware(Recorder("always", []))

    @truncated.get("/hot")
    async def hot(request: Any) -> Response:
        return Response(b"hot")

    truncated._compile_routes()

    def shape(sent: list[dict[str, Any]]) -> Any:
        return [
            (
                message.get("type"),
                message.get("status"),
                sorted(message.get("headers") or []),
                message.get("body"),
            )
            for message in sent
        ]

    assert shape(await invoke(compartmented, "/hot")) == shape(
        await invoke(truncated, "/hot")
    )


@pytest.mark.asyncio
async def test_methods_of_one_route_can_differ() -> None:
    """`applies_to` takes the method too, so POST and GET need not agree."""
    events: list[str] = []

    class PostOnly(Recorder):
        def applies_to(self, method: str, path: str) -> bool:
            return method == "POST"

    app = Wreath()
    app.add_middleware(PostOnly("post-only", events))

    @app.route("/thing", methods=["GET", "POST"])
    async def thing(request: Any) -> Response:
        return Response(b"ok")

    app._compile_routes()

    assert status_of(await invoke(app, "/thing", method="GET")) == 200
    assert events == []
    assert status_of(await invoke(app, "/thing", method="POST")) == 200
    assert events == ["post-only-in", "post-only-out"]
