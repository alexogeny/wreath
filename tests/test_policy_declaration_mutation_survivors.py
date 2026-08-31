from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wreath import policy as policy_module
from wreath.policy import AIScrapingPolicy, CsrfPolicy, HttpPolicy, RateLimitPolicy, RequestIdPolicy


class Component:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[str] = []
        self._form_field = None
        self._ingress_sync = self.ingress

    def ingress(self, request: Any) -> Any:
        self.calls.append("sync")
        return self.result

    async def _ingress(self, request: Any) -> Any:
        self.calls.append("async")
        return self.result

    async def after(self, request: Any, response: Any) -> Any:
        self.calls.append("after")
        return response

    def _ingress_scope(self, scope: Any, method: str, path: str) -> Any:
        self.calls.append(f"scope:{method}:{path}")
        return self.result

    def _egress_inplace(self, request: Any, response: Any) -> None:
        self.calls.append("egress")


def test_merge_refuses_duplicates_and_replaces_only_the_default_ai_policy() -> None:
    first_ai = AIScrapingPolicy(allow=("gptbot",))
    second_ai = AIScrapingPolicy(allow=("claudebot",))
    request_id = RequestIdPolicy()

    with pytest.raises(ValueError, match="request_id"):
        HttpPolicy(request_id=request_id).merged(HttpPolicy(request_id=RequestIdPolicy()))

    replaced = HttpPolicy(ai_scraping=first_ai)._merged(
        HttpPolicy(ai_scraping=second_ai),
        replace_default_ai=True,
    )
    assert replaced.ai_scraping is second_ai

    retained = HttpPolicy(ai_scraping=first_ai)._merged(
        HttpPolicy(),
        replace_default_ai=True,
    )
    assert retained.ai_scraping is first_ai

    with pytest.raises(ValueError, match="request_id"):
        HttpPolicy(ai_scraping=first_ai, request_id=request_id)._merged(
            HttpPolicy(ai_scraping=second_ai, request_id=RequestIdPolicy()),
            replace_default_ai=True,
        )


@pytest.mark.asyncio
async def test_reference_ingress_refuses_a_rate_limiter_without_an_ingress_stage() -> None:
    policy = HttpPolicy()
    policy.rate_limit = SimpleNamespace(_ingress_sync=None, _ingress=None)
    with pytest.raises(RuntimeError, match="no bound ingress stage"):
        await policy._reference_ingress(SimpleNamespace())


@pytest.mark.asyncio
async def test_reference_ingress_selects_both_csrf_paths_and_returns_refusals() -> None:
    sync = Component()
    sync._form_field = None
    sync_policy = HttpPolicy()
    sync_policy.csrf = sync
    request = SimpleNamespace()
    assert await sync_policy._reference_ingress(request) is None
    assert sync.calls == ["sync"]

    refusal = object()
    asynchronous = Component(refusal)
    asynchronous._form_field = "csrf"
    async_policy = HttpPolicy()
    async_policy.csrf = asynchronous
    assert await async_policy._reference_ingress(SimpleNamespace()) is refusal
    assert asynchronous.calls == ["async"]


def test_reference_scope_ingress_refuses_both_invalid_states() -> None:
    policy = HttpPolicy()
    with pytest.raises(RuntimeError, match="not scope-only"):
        policy._reference_ingress_scope({}, "GET", "/")

    policy._native_ingress_only = True
    with pytest.raises(RuntimeError, match="not scope-only"):
        policy._reference_ingress_scope({}, "GET", "/")

    policy.ai_scraping = Component()
    policy._native_ingress_only = False
    with pytest.raises(RuntimeError, match="not scope-only"):
        policy._reference_ingress_scope({}, "GET", "/")


def test_reference_scope_ingress_dispatches_to_ai_scraping() -> None:
    component = Component(result="blocked")
    policy = HttpPolicy()
    policy.ai_scraping = component
    policy._native_ingress_only = True
    assert policy._reference_ingress_scope({}, "GET", "/private") == "blocked"
    assert component.calls == ["scope:GET:/private"]


@pytest.mark.asyncio
async def test_reference_post_auth_refuses_an_unbound_limiter_and_returns_a_candidate() -> None:
    policy = HttpPolicy()
    limiter_without_ingress = RateLimitPolicy(limit=1, window=1.0)
    limiter_without_ingress._ingress_sync = None
    limiter_without_ingress._ingress = None
    policy.principal_rate_limit = limiter_without_ingress
    with pytest.raises(RuntimeError, match="no ingress stage"):
        await policy._reference_post_auth(object())

    refusal = object()
    limiter = Component(refusal)
    policy.principal_rate_limit = limiter
    assert await policy._reference_post_auth(object()) is refusal

    class Idempotency:
        async def action(self, request: Any) -> str:
            return "idempotent"

    limiter.result = None
    policy.idempotency = Idempotency()
    assert await policy._reference_post_auth(object()) == "idempotent"


@pytest.mark.asyncio
async def test_native_one_shot_still_runs_a_dynamic_cache_policy() -> None:
    cache = Component()
    cache.policy = object()
    policy = HttpPolicy()
    policy.cache_control = cache
    response = object()
    assert (
        await policy._reference_dynamic_egress(
            object(),
            response,
            native_one_shot=True,
        )
        is response
    )
    assert cache.calls == ["after"]


def test_reference_action_exit_releases_only_a_configured_component() -> None:
    calls: list[str] = []
    HttpPolicy()._reference_action_exit()
    policy = HttpPolicy()
    policy.concurrency = SimpleNamespace(_release=lambda: calls.append("release"))
    policy._reference_action_exit()
    assert calls == ["release"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mask", "attribute", "message"),
    [
        (policy_module._SECURITY, "security_headers", "security"),
        (policy_module._CSRF, "csrf", "CSRF"),
        (policy_module._CORS, "cors", "CORS"),
        (policy_module._TIMING, "server_timing", "timing"),
        (policy_module._REQUEST_ID, "request_id", "request-ID"),
    ],
)
async def test_reference_egress_refuses_a_mask_without_its_component(
    mask: int,
    attribute: str,
    message: str,
) -> None:
    policy = HttpPolicy()
    assert getattr(policy, attribute) is None
    with pytest.raises(RuntimeError, match=message):
        await policy._reference_egress(SimpleNamespace(_policy_mask=mask), object())


@pytest.mark.asyncio
async def test_reference_websocket_runs_optional_stages_and_stops_on_a_refusal() -> None:
    proxy = Component()
    trusted = Component()
    ai = Component()
    traffic_refusal = object()
    traffic = Component(traffic_refusal)
    origin = Component(result="origin")
    policy = HttpPolicy()
    policy.proxy = proxy
    policy.trusted_host = trusted
    policy.ai_scraping = ai
    policy.traffic = traffic
    policy.websocket_origin = origin

    assert await policy._reference_websocket(object()) is traffic_refusal
    assert proxy.calls == ["sync"]
    assert trusted.calls == ["sync"]
    assert ai.calls == ["sync"]
    assert traffic.calls == ["sync"]
    assert origin.calls == []

    ai.result = "ai-refusal"
    traffic.calls.clear()
    assert await policy._reference_websocket(object()) == "ai-refusal"
    assert traffic.calls == []


def _rate(**overrides: Any):
    key = object()
    values = {
        "_exempt": None,
        "_quota": None,
        "_key": key,
        "_default_key": key,
        "_store": SimpleNamespace(_bucket=object()),
        "_cost": 1,
        "_policy_headers": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "rate",
    [
        _rate(_exempt=lambda request: False),
        _rate(_quota=lambda request: 1),
        _rate(_key=object()),
        _rate(_store=SimpleNamespace()),
    ],
)
def test_native_freeze_refuses_each_non_native_rate_limit_shape(rate: Any) -> None:
    policy = HttpPolicy()
    policy.rate_limit = rate
    assert policy._freeze_native() is None


def test_native_freeze_checks_every_non_native_rate_limit_condition() -> None:
    policy = HttpPolicy()
    for rate in (
        _rate(_exempt=lambda request: False),
        _rate(_quota=lambda request: 1),
        _rate(_key=object()),
        _rate(_store=SimpleNamespace()),
    ):
        policy.rate_limit = rate
        assert policy._freeze_native() is None


def test_native_freeze_accepts_the_default_memory_rate_shape() -> None:
    policy = HttpPolicy()
    policy.rate_limit = _rate()
    descriptor = policy._freeze_native()
    assert descriptor is not None
    assert descriptor[4][1:] == (1, True, policy.rate_limit)


@pytest.mark.parametrize(
    "csrf",
    [
        SimpleNamespace(_exempt=lambda request: False, _form_field=None),
        SimpleNamespace(_exempt=None, _form_field="csrf"),
    ],
)
def test_native_freeze_refuses_each_dynamic_csrf_shape(csrf: Any) -> None:
    policy = HttpPolicy()
    policy.csrf = csrf
    assert policy._freeze_native() is None


def test_native_freeze_checks_both_dynamic_csrf_conditions_and_accepts_static() -> None:
    policy = HttpPolicy()
    policy.csrf = CsrfPolicy("s" * 32, exempt=lambda request: False)
    assert policy._freeze_native() is None
    policy.csrf = CsrfPolicy("s" * 32, form_field="csrf")
    assert policy._freeze_native() is None
    policy.csrf = CsrfPolicy("s" * 32)
    descriptor = policy._freeze_native()
    assert descriptor is not None
    assert descriptor[8] is not None


def test_native_freeze_distinguishes_nonce_and_static_security_headers() -> None:
    policy = HttpPolicy()
    policy.security_headers = SimpleNamespace(_has_nonce=True)
    assert policy._freeze_native() is None

    policy.security_headers = SimpleNamespace(
        _has_nonce=False,
        headers=((b"x-frame-options", b"DENY"),),
        https_headers=(),
    )
    descriptor = policy._freeze_native()
    assert descriptor is not None
    assert descriptor[9] == (policy.security_headers.headers, ())


@pytest.mark.parametrize(
    ("default", "dynamic", "expected"),
    [
        (None, None, None),
        (SimpleNamespace(to_header=lambda: b"public", public=True), object(), None),
        (
            SimpleNamespace(to_header=lambda: b"public", public=True),
            None,
            (b"public", True),
        ),
    ],
)
def test_native_freeze_distinguishes_cache_defaults_and_dynamic_policy(
    default: Any,
    dynamic: Any,
    expected: Any,
) -> None:
    policy = HttpPolicy()
    policy.cache_control = SimpleNamespace(default=default, policy=dynamic)
    descriptor = policy._freeze_native()
    assert descriptor is not None
    assert descriptor[11] == expected
