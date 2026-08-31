from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from wreath import policy as policy_module
from wreath.policy import HttpPolicy


class RecordingComponent:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        result: Any = None,
        policy: Any = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.result = result
        self.policy = policy
        self._form_field = None
        self._ingress_sync = self.ingress_sync

    def _prepare_nonce(self, request: Any) -> None:
        self.calls.append(f"{self.name}.prepare")

    def ingress_sync(self, request: Any) -> Any:
        self.calls.append(f"{self.name}.ingress")
        return self.result

    async def _ingress(self, request: Any) -> Any:
        self.calls.append(f"{self.name}.ingress")
        return self.result

    async def before(self, request: Any) -> None:
        self.calls.append(f"{self.name}.before")

    async def after(self, request: Any, response: Any) -> Any:
        self.calls.append(f"{self.name}.after")
        return response

    def _egress_inplace(self, request: Any, response: Any) -> None:
        self.calls.append(f"{self.name}.egress")


async def test_reference_ingress_runs_every_configured_stage_in_order() -> None:
    calls: list[str] = []
    policy = HttpPolicy()
    for name in (
        "security_headers",
        "proxy",
        "trusted_host",
        "maintenance",
        "ai_scraping",
        "traffic",
        "rate_limit",
        "signed_routes",
        "request_decompression",
        "request_id",
        "server_timing",
        "cors",
        "csrf",
    ):
        setattr(policy, name, RecordingComponent(name, calls))
    request = SimpleNamespace()

    assert await policy._reference_ingress(request) is None
    assert calls == [
        "security_headers.prepare",
        "proxy.ingress",
        "trusted_host.ingress",
        "maintenance.ingress",
        "ai_scraping.ingress",
        "traffic.ingress",
        "rate_limit.ingress",
        "signed_routes.ingress",
        "request_decompression.ingress",
        "request_id.ingress",
        "server_timing.ingress",
        "cors.ingress",
        "csrf.ingress",
    ]
    assert request._policy_mask == (
        policy_module._SECURITY
        | policy_module._PROXY
        | policy_module._TRUSTED_HOST
        | policy_module._RATE_LIMIT
        | policy_module._REQUEST_ID
        | policy_module._TIMING
        | policy_module._CORS
        | policy_module._CSRF
    )


async def test_reference_ingress_with_no_components_has_an_empty_mask() -> None:
    request = SimpleNamespace()

    assert await HttpPolicy()._reference_ingress(request) is None
    assert request._policy_mask == 0


async def test_reference_ingress_returns_an_ai_scraping_refusal_immediately() -> None:
    calls: list[str] = []
    refusal = object()
    policy = HttpPolicy()
    policy.ai_scraping = RecordingComponent(
        "ai_scraping", calls, result=refusal
    )
    policy.traffic = RecordingComponent("traffic", calls)

    assert await policy._reference_ingress(SimpleNamespace()) is refusal
    assert calls == ["ai_scraping.ingress"]


async def test_reference_activation_runs_only_a_configured_session() -> None:
    calls: list[str] = []
    request = SimpleNamespace()
    absent = HttpPolicy()
    configured = HttpPolicy()
    configured.session = RecordingComponent("session", calls)

    await absent._reference_activation(request)
    await configured._reference_activation(request)

    assert calls == ["session.before"]


async def test_reference_websocket_proxy_trusted_host_traffic_and_origin_are_optional() -> None:
    assert await HttpPolicy()._reference_websocket(SimpleNamespace()) is None


async def test_reference_dynamic_egress_runs_every_configured_stage_in_order() -> None:
    calls: list[str] = []
    policy = HttpPolicy()
    policy.idempotency = RecordingComponent("idempotency", calls)
    policy.session = RecordingComponent("session", calls)
    policy.cache_control = RecordingComponent("cache_control", calls)
    policy.compression = RecordingComponent("compression", calls)
    response = object()

    assert await policy._reference_dynamic_egress(object(), response) is response
    assert calls == [
        "idempotency.after",
        "session.after",
        "cache_control.after",
        "compression.after",
    ]


async def test_reference_dynamic_egress_skips_empty_and_native_stages() -> None:
    calls: list[str] = []
    response = object()
    absent = HttpPolicy()
    native = HttpPolicy()
    native.cache_control = RecordingComponent("cache_control", calls)
    native.compression = RecordingComponent("compression", calls)

    assert await absent._reference_dynamic_egress(object(), response) is response
    assert (
        await native._reference_dynamic_egress(
            object(), response, native_one_shot=True
        )
        is response
    )
    assert calls == []


async def test_reference_egress_runs_exactly_the_stages_in_the_request_mask() -> None:
    calls: list[str] = []
    policy = HttpPolicy()
    policy.security_headers = RecordingComponent("security_headers", calls)
    policy.csrf = RecordingComponent("csrf", calls)
    policy.cors = RecordingComponent("cors", calls)
    policy.server_timing = RecordingComponent("server_timing", calls)
    policy.request_id = RecordingComponent("request_id", calls)
    mask = (
        policy_module._SECURITY
        | policy_module._CSRF
        | policy_module._CORS
        | policy_module._TIMING
        | policy_module._REQUEST_ID
    )
    request = SimpleNamespace(_policy_mask=mask)
    response = object()

    assert await policy._reference_egress(request, response) is response
    assert calls == [
        "security_headers.egress",
        "csrf.egress",
        "cors.egress",
        "server_timing.egress",
        "request_id.egress",
    ]


async def test_reference_egress_with_an_empty_mask_runs_no_stage() -> None:
    response = object()

    assert (
        await HttpPolicy()._reference_egress(
            SimpleNamespace(_policy_mask=0), response
        )
        is response
    )
