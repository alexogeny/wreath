from __future__ import annotations

import asyncio
import inspect
import random
from typing import Any

import pytest

import wreath.app as app_module
from wreath import Router, Wreath
from wreath.middleware import (
    MiddlewareHooks,
    MiddlewareTape,
)
from wreath.policy import (
    CorsPolicy,
    CsrfPolicy,
    HttpPolicy,
    ProxyPolicy,
    RateLimitPolicy,
    RequestIdPolicy,
    SecurityHeadersPolicy,
    ServerTimingPolicy,
    TrustedHostPolicy,
)
from wreath.response import TextResponse


async def invoke(app: Wreath, path: str = "/") -> list[dict[str, Any]]:
    messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""},
        receive,
        send,
    )
    return sent


def sync_marker(events: list[str], name: str, *, respond: Any = None) -> MiddlewareHooks:
    """A synchronous (fusable) middleware that records it ran, optionally
    short-circuiting with `respond`."""

    def before_sync(request: Any) -> Any:
        events.append(name)
        return respond

    return MiddlewareHooks(before_sync=before_sync)


def sync_marker_with_after(events: list[str], name: str, *, respond: Any = None) -> MiddlewareHooks:
    def before_sync(request: Any) -> Any:
        events.append(f"{name}-in")
        return respond

    async def after(request: Any, response: Any) -> Any:
        events.append(f"{name}-out")
        return response

    return MiddlewareHooks(before_sync=before_sync, after=after)


def sync_marker_with_after_sync(
    events: list[str], name: str, *, respond: Any = None
) -> MiddlewareHooks:
    def before_sync(request: Any) -> Any:
        events.append(f"{name}-in")
        return respond

    def after_sync(request: Any, response: Any) -> Any:
        events.append(f"{name}-out")
        return response

    async def after(request: Any, response: Any) -> Any:
        events.append(f"{name}-async-out")
        return response

    return MiddlewareHooks(before_sync=before_sync, after=after, after_sync=after_sync)


def sync_marker_with_after_inplace(events: list[str], name: str, *, respond: Any = None) -> Any:
    class InPlaceMarker:
        def before_sync(self, request: Any) -> Any:
            events.append(f"{name}-in")
            return respond

        def after_inplace(self, request: Any, response: Any) -> None:
            events.append(f"{name}-out")

        def after_sync(self, request: Any, response: Any) -> Any:
            events.append(f"{name}-wrong-sync-out")
            return response

        async def after(self, request: Any, response: Any) -> Any:
            events.append(f"{name}-wrong-async-out")
            return response

    return InPlaceMarker()


class Magic8Ball:
    """A gloriously unnecessary *async* user middleware. On the way in it awaits
    the event loop, consults a seeded magic 8-ball, records the verdict, and if
    the vibes are off refuses the request with 418. Being async, it cannot be
    fused -- so it must partition the native sync runs around it."""

    ANSWERS = (
        "It is certain",
        "Without a doubt",
        "Reply hazy try again",
        "Outlook not so good",
        "Very doubtful",
    )
    BAD_VIBES = "Outlook not so good"

    def __init__(self, events: list[str], seed: int) -> None:
        self._events = events
        self._rng = random.Random(seed)

    async def before(self, request: Any) -> Any:
        await asyncio.sleep(0)  # genuinely async: yield to the loop
        verdict = self._rng.choice(self.ANSWERS)
        self._events.append(f"8ball:{verdict}")
        if verdict == self.BAD_VIBES:
            return TextResponse("the vibes are off, come back later", status=418)
        return None


def _seed_for(verdict: str) -> int:
    for seed in range(10_000):
        if random.Random(seed).choice(Magic8Ball.ANSWERS) == verdict:
            return seed
    raise AssertionError(f"no seed produced {verdict!r}")


@pytest.mark.asyncio
async def test_contiguous_before_sync_hooks_fuse_into_one_instruction() -> None:
    app = Wreath()
    events: list[str] = []

    @app.get(
        "/",
        middleware=(
            sync_marker(events, "a"),
            sync_marker(events, "b"),
            sync_marker(events, "c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    matched = app.router.match("GET", "/")

    assert isinstance(matched[0], MiddlewareTape)
    # Three synchronous befores collapse to a single fused instruction.
    assert matched[0].operations == ("fused_before", "endpoint")
    assert sent[1]["body"] == b"ok"
    assert events == ["a", "b", "c", "endpoint"]


@pytest.mark.asyncio
async def test_before_sync_hooks_are_never_awaited() -> None:
    # before_sync is a PLAIN function, not a coroutine function. If the tape
    # tried to await it, awaiting its None return would raise TypeError, so a
    # clean run is proof the fused pass calls it synchronously.
    app = Wreath()
    events: list[str] = []
    mw = sync_marker(events, "sync")
    assert not inspect.iscoroutinefunction(mw.before_sync)

    @app.get("/", middleware=(mw,))
    async def endpoint(request: Any) -> str:
        return "ok"

    sent = await invoke(app)
    assert sent[1]["body"] == b"ok"
    assert events == ["sync"]


@pytest.mark.asyncio
async def test_before_sync_short_circuit_runs_entered_afters_only() -> None:
    app = Wreath()
    events: list[str] = []

    @app.get(
        "/",
        middleware=(
            sync_marker_with_after(events, "a"),
            sync_marker_with_after(events, "b", respond=TextResponse("stop", status=403)),
            sync_marker_with_after(events, "c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "never"

    sent = await invoke(app)

    assert sent[0]["status"] == 403
    assert sent[1]["body"] == b"stop"
    # 'c' never runs; the endpoint never runs; the after hooks of the entered
    # middleware (a, b) run in reverse order on the short-circuit response.
    assert events == ["a-in", "b-in", "b-out", "a-out"]


@pytest.mark.asyncio
async def test_after_sync_hooks_compile_without_await_in_reverse_order() -> None:
    app = Wreath()
    events: list[str] = []

    @app.get(
        "/",
        middleware=(
            sync_marker_with_after_sync(events, "a"),
            sync_marker_with_after_sync(events, "b"),
            sync_marker_with_after_sync(events, "c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    matched = app.router.match("GET", "/")

    assert isinstance(matched[0], MiddlewareTape)
    assert matched[0].operations == (
        "fused_before",
        "endpoint",
        "after_sync",
        "after_sync",
        "after_sync",
    )
    assert sent[1]["body"] == b"ok"
    assert events == [
        "a-in",
        "b-in",
        "c-in",
        "endpoint",
        "c-out",
        "b-out",
        "a-out",
    ]


@pytest.mark.asyncio
async def test_after_sync_short_circuit_runs_entered_afters_only() -> None:
    app = Wreath()
    events: list[str] = []

    @app.get(
        "/",
        middleware=(
            sync_marker_with_after_sync(events, "a"),
            sync_marker_with_after_sync(events, "b", respond=TextResponse("stop", status=403)),
            sync_marker_with_after_sync(events, "c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "never"

    sent = await invoke(app)

    assert sent[0]["status"] == 403
    assert sent[1]["body"] == b"stop"
    assert events == ["a-in", "b-in", "b-out", "a-out"]


@pytest.mark.asyncio
async def test_after_inplace_preserves_response_without_using_return_value() -> None:
    app = Wreath()
    events: list[str] = []

    @app.get(
        "/",
        middleware=(
            sync_marker_with_after_inplace(events, "a"),
            sync_marker_with_after_inplace(events, "b"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    matched = app.router.match("GET", "/")

    assert isinstance(matched[0], MiddlewareTape)
    assert matched[0].operations == (
        "fused_before",
        "endpoint",
        "after_inplace",
        "after_inplace",
    )
    assert sent[1]["body"] == b"ok"
    assert events == ["a-in", "b-in", "endpoint", "b-out", "a-out"]


@pytest.mark.asyncio
async def test_after_inplace_short_circuit_unwinds_only_entered_hooks() -> None:
    app = Wreath()
    events: list[str] = []

    @app.get(
        "/",
        middleware=(
            sync_marker_with_after_inplace(events, "a"),
            sync_marker_with_after_inplace(events, "b", respond=TextResponse("stop", status=403)),
            sync_marker_with_after_inplace(events, "c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "never"

    sent = await invoke(app)

    assert sent[0]["status"] == 403
    assert events == ["a-in", "b-in", "b-out", "a-out"]


@pytest.mark.asyncio
async def test_async_user_middleware_partitions_the_fused_runs() -> None:
    app = Wreath()
    events: list[str] = []
    good_seed = _seed_for("It is certain")

    @app.get(
        "/",
        middleware=(
            sync_marker(events, "native-a"),
            sync_marker(events, "native-b"),
            Magic8Ball(events, good_seed),  # async: breaks the fused run
            sync_marker(events, "native-c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    matched = app.router.match("GET", "/")

    # a,b fuse; the 8-ball is an async `before`; c fuses again after it.
    assert matched[0].operations == ("fused_before", "before", "fused_before", "endpoint")
    assert sent[1]["body"] == b"ok"
    assert events == [
        "native-a",
        "native-b",
        "8ball:It is certain",
        "native-c",
        "endpoint",
    ]


@pytest.mark.asyncio
async def test_magic_8ball_short_circuits_between_fused_runs() -> None:
    app = Wreath()
    events: list[str] = []
    bad_seed = _seed_for(Magic8Ball.BAD_VIBES)

    @app.get(
        "/",
        middleware=(
            sync_marker(events, "native-a"),
            sync_marker(events, "native-b"),
            Magic8Ball(events, bad_seed),
            sync_marker(events, "native-c"),
        ),
    )
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "never"

    sent = await invoke(app)

    assert sent[0]["status"] == 418
    assert sent[1]["body"] == b"the vibes are off, come back later"
    # The second fused run (native-c) and the endpoint never execute.
    assert events == ["native-a", "native-b", f"8ball:{Magic8Ball.BAD_VIBES}"]


@pytest.mark.asyncio
async def test_fused_sync_matches_unfused_async_behaviour() -> None:
    # The same logical chain, once with before_sync (fused) and once with an
    # equivalent async before (unfused), must produce identical events + body.
    def build(app: Wreath, events: list[str], *, sync: bool) -> None:
        def make(name: str) -> MiddlewareHooks:
            if sync:
                return sync_marker(events, name)

            async def before(request: Any) -> Any:
                events.append(name)
                return None

            return MiddlewareHooks(before=before)

        @app.get("/", middleware=(make("a"), make("b"), make("c")))
        async def endpoint(request: Any) -> str:
            events.append("endpoint")
            return "ok"

    fused_events: list[str] = []
    fused_app = Wreath()
    build(fused_app, fused_events, sync=True)
    fused_sent = await invoke(fused_app)

    plain_events: list[str] = []
    plain_app = Wreath()
    build(plain_app, plain_events, sync=False)
    plain_sent = await invoke(plain_app)

    assert fused_events == plain_events == ["a", "b", "c", "endpoint"]
    assert fused_sent[1]["body"] == plain_sent[1]["body"] == b"ok"
    # And the fused one really did fuse, while the plain one did not.
    assert fused_app.router.match("GET", "/")[0].operations == ("fused_before", "endpoint")
    assert plain_app.router.match("GET", "/")[0].operations == (
        "before",
        "before",
        "before",
        "endpoint",
    )


# Built-in middleware (host checks, rate-limit, CSRF) are global_scope, so they
# run through the app's global-hook loop, not the per-route tape. That loop must
# dispatch a synchronous before_sync hook WITHOUT awaiting it (no coroutine),
# while still awaiting genuinely-async before hooks -- the same partition.


class GlobalSync:
    """A global middleware whose before hook is synchronous (fusable)."""

    global_scope = True

    def __init__(
        self, events: list[str], name: str, *, respond: Any = None, with_after: bool = False
    ) -> None:
        self._events, self._name, self._respond = events, name, respond
        if with_after:
            self.after = self._after

    def before_sync(self, request: Any) -> Any:
        self._events.append(f"{self._name}-in")
        return self._respond

    async def _after(self, request: Any, response: Any) -> Any:
        self._events.append(f"{self._name}-out")
        return response


class GlobalAsync:
    """A global middleware whose before hook genuinely awaits (not fusable)."""

    global_scope = True

    def __init__(self, events: list[str], name: str) -> None:
        self._events, self._name = events, name

    async def before(self, request: Any) -> Any:
        await asyncio.sleep(0)
        self._events.append(f"{self._name}-in")
        return None


class GlobalSyncAfter:
    """A global middleware whose two hooks are both plain functions."""

    global_scope = True

    def __init__(self, events: list[str], name: str, *, respond: Any = None) -> None:
        self._events, self._name, self._respond = events, name, respond

    def before_sync(self, request: Any) -> Any:
        self._events.append(f"{self._name}-in")
        return self._respond

    def after_sync(self, request: Any, response: Any) -> Any:
        self._events.append(f"{self._name}-out")
        return response

    async def after(self, request: Any, response: Any) -> Any:
        self._events.append(f"{self._name}-async-out")
        return response


class GlobalInPlaceAfter(GlobalSyncAfter):
    def after_inplace(self, request: Any, response: Any) -> None:
        self._events.append(f"{self._name}-out")

    def after_sync(self, request: Any, response: Any) -> Any:
        self._events.append(f"{self._name}-wrong-sync-out")
        return response


class GlobalBeforeOnly:
    global_scope = True

    def before_sync(self, request: Any) -> None:
        return None


class GlobalAfterOnly:
    global_scope = True

    def after_inplace(self, request: Any, response: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_global_before_sync_runs_without_await() -> None:
    app = Wreath()
    events: list[str] = []
    mw = GlobalSync(events, "g")
    assert not inspect.iscoroutinefunction(mw.before_sync)
    app.add_middleware(mw)

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    assert sent[1]["body"] == b"ok"
    assert events == ["g-in", "endpoint"]


@pytest.mark.asyncio
async def test_global_sync_short_circuit_runs_entered_afters() -> None:
    app = Wreath()
    events: list[str] = []
    app.add_middleware(GlobalSync(events, "a", with_after=True))
    app.add_middleware(
        GlobalSync(events, "b", respond=TextResponse("stop", status=403), with_after=True)
    )
    app.add_middleware(GlobalSync(events, "c", with_after=True))

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "never"

    sent = await invoke(app)
    assert sent[0]["status"] == 403
    assert sent[1]["body"] == b"stop"
    # c never runs; endpoint never runs; entered afters (a, b) run in reverse.
    assert events == ["a-in", "b-in", "b-out", "a-out"]


@pytest.mark.asyncio
async def test_global_sync_and_async_hooks_interleave_in_order() -> None:
    app = Wreath()
    events: list[str] = []
    app.add_middleware(GlobalSync(events, "sync-a"))
    app.add_middleware(GlobalAsync(events, "async-b"))
    app.add_middleware(GlobalSync(events, "sync-c"))

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    assert sent[1]["body"] == b"ok"
    assert events == ["sync-a-in", "async-b-in", "sync-c-in", "endpoint"]


@pytest.mark.asyncio
async def test_global_after_sync_runs_without_await_and_in_reverse_order() -> None:
    app = Wreath()
    events: list[str] = []
    outer = GlobalSyncAfter(events, "outer")
    inner = GlobalSyncAfter(events, "inner")
    assert not inspect.iscoroutinefunction(outer.after_sync)
    app.add_middleware(outer)
    app.add_middleware(inner)

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    assert sent[1]["body"] == b"ok"
    assert events == ["outer-in", "inner-in", "endpoint", "inner-out", "outer-out"]


@pytest.mark.asyncio
async def test_global_after_sync_preserves_short_circuit_unwind() -> None:
    app = Wreath()
    events: list[str] = []
    app.add_middleware(GlobalSyncAfter(events, "a"))
    app.add_middleware(GlobalSyncAfter(events, "b", respond=TextResponse("stop", status=403)))
    app.add_middleware(GlobalSyncAfter(events, "c"))

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "never"

    sent = await invoke(app)
    assert sent[0]["status"] == 403
    assert events == ["a-in", "b-in", "b-out", "a-out"]


@pytest.mark.asyncio
async def test_global_after_inplace_preserves_response_and_precedence() -> None:
    app = Wreath()
    events: list[str] = []
    app.add_middleware(GlobalInPlaceAfter(events, "outer"))
    app.add_middleware(GlobalInPlaceAfter(events, "inner"))

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)

    assert sent[1]["body"] == b"ok"
    assert events == ["outer-in", "inner-in", "endpoint", "inner-out", "outer-out"]


@pytest.mark.asyncio
async def test_global_compiler_builds_dense_directional_hook_plans() -> None:
    app = Wreath()
    app.add_middleware(GlobalAfterOnly())
    app.add_middleware(GlobalBeforeOnly())
    app.add_middleware(GlobalInPlaceAfter([], "both"))

    @app.get("/")
    async def endpoint(request: Any) -> str:
        return "ok"

    await invoke(app)

    assert len(app._global_before_hooks) == 2
    assert len(app._global_after_hooks) == 2


@pytest.mark.asyncio
async def test_response_only_route_skips_the_middleware_coercion_wrapper(
    monkeypatch,
) -> None:
    calls = 0
    original = app_module._coerce_response

    def counted(value: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(app_module, "_coerce_response", counted)
    app = Wreath()

    @app.get(
        "/",
        middleware=(MiddlewareHooks(before_sync=lambda request: None),),
        response_only=True,
    )
    async def endpoint(request: Any) -> TextResponse:
        return TextResponse("ok")

    sent = await invoke(app)

    assert sent[1]["body"] == b"ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_included_router_preserves_the_response_only_contract(
    monkeypatch,
) -> None:
    calls = 0
    original = app_module._coerce_response

    def counted(value: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(app_module, "_coerce_response", counted)
    router = Router(middleware=(MiddlewareHooks(before_sync=lambda request: None),))

    @router.get("/", response_only=True)
    async def endpoint(request: Any) -> TextResponse:
        return TextResponse("ok")

    app = Wreath()
    app.include_router(router, prefix="/included")
    sent = await invoke(app, "/included/")

    assert sent[1]["body"] == b"ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_reused_response_does_not_accumulate_observability_headers() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            request_id=RequestIdPolicy(),
            server_timing=ServerTimingPolicy(),
            security_headers=SecurityHeadersPolicy(),
        )
    )
    response = TextResponse("ok")

    @app.get("/", response_only=True)
    async def endpoint(request: Any) -> TextResponse:
        return response

    for _ in range(20):
        await invoke(app)

    names = [name for name, _value in response.headers]
    assert names.count(b"x-request-id") == 1
    assert names.count(b"server-timing") == 1


@pytest.mark.asyncio
async def test_reused_response_replaces_its_csrf_cookie() -> None:
    app = Wreath(http_policy=HttpPolicy(csrf=CsrfPolicy("x" * 32, secure=False)))
    response = TextResponse("ok")

    @app.get("/", response_only=True)
    async def endpoint(request: Any) -> TextResponse:
        return response

    for _ in range(20):
        await invoke(app)

    cookies = [value for name, value in response.headers if name == b"set-cookie"]
    assert len(cookies) == 1
    assert cookies[0].startswith(b"wreath_csrf=")


def test_trusted_host_exposes_no_public_hook_protocol() -> None:
    policy = TrustedHostPolicy(("example.com",))
    assert not hasattr(policy, "before_sync")
    assert not hasattr(policy, "before")


def test_rate_limit_exposes_no_public_hook_protocol() -> None:
    local = RateLimitPolicy(limit=5)
    assert not hasattr(local, "before_sync")
    assert not hasattr(local, "before")


@pytest.mark.parametrize(
    ("middleware", "hooks"),
    (
        pytest.param(
            lambda: ProxyPolicy(trusted=("127.0.0.1",)),
            ("before", "before_sync", "after", "after_inplace"),
            id="proxy",
        ),
        pytest.param(
            lambda: CorsPolicy(allow_origins=("https://example.com",)),
            ("before", "before_sync", "after", "after_inplace"),
            id="cors",
        ),
        pytest.param(
            lambda: CsrfPolicy("x" * 32, secure=False),
            ("before", "before_sync", "after", "after_inplace"),
            id="csrf",
        ),
        pytest.param(
            SecurityHeadersPolicy,
            ("before", "before_sync", "after", "after_inplace"),
            id="security-headers",
        ),
        pytest.param(
            RequestIdPolicy,
            ("before", "before_sync", "after", "after_inplace"),
            id="request-id",
        ),
        pytest.param(
            ServerTimingPolicy,
            ("before", "before_sync", "after", "after_inplace"),
            id="server-timing",
        ),
    ),
)
def test_policy_builtins_expose_no_public_hooks(middleware: Any, hooks: tuple[str, ...]) -> None:
    instance = middleware()
    for name in hooks:
        assert not hasattr(instance, name)


@pytest.mark.asyncio
async def test_trusted_host_and_rate_limit_still_enforce_through_the_pipeline() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            trusted_host=TrustedHostPolicy(("allowed.test",)),
            rate_limit=RateLimitPolicy(limit=1),
        )
    )

    @app.get("/")
    async def endpoint(request: Any) -> str:
        return "ok"

    async def call(host: str) -> list[dict[str, Any]]:
        messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(messages)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"host", host.encode())],
                "query_string": b"",
                "client": ("1.2.3.4", 5678),
            },
            receive,
            send,
        )
        return sent

    # Wrong host is rejected synchronously by the trusted-host sync hook.
    bad = await call("evil.test")
    assert bad[0]["status"] == 400
    # Allowed host passes once, then the rate-limit sync hook trips on the 2nd.
    assert (await call("allowed.test"))[0]["status"] == 200
    assert (await call("allowed.test"))[0]["status"] == 429
