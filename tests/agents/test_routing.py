from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

from wreath._agents.core import (
    BackplaneError,
    ModelBackplane,
    ModelMessage,
    ModelRequest,
    ModelResponseEvent,
    ModelTarget,
    ToolSpecification,
)
from wreath._agents.routing import ModelCandidate, ModelRoutePolicy, RoutedBackplane


@dataclass
class Backplane:
    name: str
    outcomes: list[list[ModelResponseEvent] | Exception]
    requests: list[ModelRequest] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> Any:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for event in outcome:
            yield event


def candidate(
    name: str,
    plane: ModelBackplane,
    *,
    model: str | None = None,
    capabilities: frozenset[str] = frozenset({"chat", "tools"}),
    regions: frozenset[str] = frozenset({"au"}),
) -> ModelCandidate:
    return ModelCandidate(
        name=name,
        target=ModelTarget(plane, model or f"{name}-model"),
        capabilities=capabilities,
        regions=regions,
    )


def policy(
    tenant: str,
    *candidates: str,
    required_capabilities: frozenset[str] = frozenset({"chat"}),
    allowed_regions: frozenset[str] = frozenset({"au"}),
) -> ModelRoutePolicy:
    return ModelRoutePolicy(
        tenant=tenant,
        candidates=candidates,
        required_capabilities=required_capabilities,
        allowed_regions=allowed_regions,
    )


def request(
    tenant: str = "acme",
    *,
    required_capabilities: frozenset[str] | None = None,
    allowed_regions: frozenset[str] | None = None,
) -> ModelRequest:
    messages = (ModelMessage("user", "hello"),)
    tools = (
        ToolSpecification(
            "weather",
            "Read weather",
            {"type": "object", "properties": {}},
        ),
    )
    metadata: dict[str, Any] = {"tenant": tenant, "trace": "trace-7"}
    if required_capabilities is not None:
        metadata["required_capabilities"] = required_capabilities
    if allowed_regions is not None:
        metadata["allowed_regions"] = allowed_regions
    return ModelRequest(
        "profile-placeholder",
        messages,
        tools,
        max_output_tokens=64,
        temperature=0.2,
        metadata=metadata,
    )


def router(
    *candidates: ModelCandidate,
    policies: tuple[ModelRoutePolicy, ...] | None = None,
    health: Any = None,
) -> RoutedBackplane:
    selected_policies = policies or (policy("acme", *(item.name for item in candidates)),)
    return RoutedBackplane(candidates, selected_policies, health=health)


def test_construction_compiles_candidates_and_tenant_policy_once() -> None:
    plane = Backplane("one", [[]])
    route = router(candidate("primary", plane))

    assert route.name == "routed"
    assert route.candidates == ("primary",)
    assert route.tenants == frozenset({"acme"})


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: RoutedBackplane(
                (candidate("same", Backplane("a", [[]])), candidate("same", Backplane("b", [[]]))),
                (policy("acme", "same"),),
            ),
            "duplicate model candidate 'same'",
        ),
        (
            lambda: RoutedBackplane(
                (candidate("one", Backplane("a", [[]])),),
                (policy("acme", "missing"),),
            ),
            "tenant 'acme'.*unknown candidate 'missing'",
        ),
        (
            lambda: RoutedBackplane(
                (candidate("one", Backplane("a", [[]])),),
                (policy("acme", "one"), policy("acme", "one")),
            ),
            "duplicate model route policy for tenant 'acme'",
        ),
        (
            lambda: RoutedBackplane(
                (
                    candidate(
                        "one",
                        Backplane("a", [[]]),
                        capabilities=frozenset({"chat"}),
                        regions=frozenset({"us"}),
                    ),
                ),
                (policy("acme", "one"),),
            ),
            "tenant 'acme'.*no statically eligible candidate",
        ),
    ],
)
def test_invalid_routing_graphs_refuse_at_construction(build: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: candidate("", Backplane("a", [[]])),
        lambda: candidate("one", Backplane("a", [[]]), capabilities=frozenset()),
        lambda: candidate("one", Backplane("a", [[]]), capabilities=frozenset({""})),
        lambda: candidate("one", Backplane("a", [[]]), regions=frozenset()),
        lambda: candidate("one", Backplane("a", [[]]), regions=frozenset({""})),
        lambda: policy("", "one"),
        lambda: policy("acme"),
        lambda: policy("acme", "one", required_capabilities=frozenset()),
        lambda: policy("acme", "one", required_capabilities=frozenset({""})),
        lambda: policy("acme", "one", allowed_regions=frozenset()),
        lambda: policy("acme", "one", allowed_regions=frozenset({""})),
    ],
)
def test_empty_candidate_and_policy_facts_refuse(build: Any) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    ("build", "error", "message"),
    [
        (lambda: candidate(cast(Any, 1), Backplane("a", [[]])), ValueError, "candidate name"),
        (
            lambda: ModelCandidate(
                "one",
                cast(Any, None),
                frozenset({"chat"}),
                frozenset({"au"}),
            ),
            TypeError,
            "target",
        ),
        (
            lambda: candidate(
                "one",
                Backplane("a", [[]]),
                capabilities=cast(Any, "chat"),
            ),
            ValueError,
            "capabilities",
        ),
        (lambda: policy(cast(Any, 1), "one"), ValueError, "policy tenant"),
        (lambda: policy("acme", ""), ValueError, "candidate names"),
        (lambda: policy("acme", cast(Any, 1)), ValueError, "candidate names"),
        (lambda: policy("acme", "one", "one"), ValueError, "duplicate candidate"),
        (lambda: RoutedBackplane((), ()), ValueError, "at least one model candidate"),
        (
            lambda: RoutedBackplane(cast(Any, (None,)), ()),
            TypeError,
            "ModelCandidate values",
        ),
        (
            lambda: RoutedBackplane((candidate("one", Backplane("a", [[]])),), ()),
            ValueError,
            "at least one tenant policy",
        ),
        (
            lambda: RoutedBackplane(
                (candidate("one", Backplane("a", [[]])),),
                cast(Any, (None,)),
            ),
            TypeError,
            "ModelRoutePolicy values",
        ),
        (
            lambda: RoutedBackplane(
                (candidate("one", Backplane("a", [[]])),),
                (policy("acme", "one"),),
                health=cast(Any, False),
            ),
            TypeError,
            "health must be callable",
        ),
    ],
)
def test_malformed_routing_declarations_refuse(
    build: Any,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        build()


def test_static_policy_checks_capability_and_region_independently() -> None:
    plane = Backplane("provider", [[]])
    capability_mismatch = candidate(
        "capability-mismatch",
        plane,
        capabilities=frozenset({"chat"}),
        regions=frozenset({"au"}),
    )
    region_mismatch = candidate(
        "region-mismatch",
        plane,
        capabilities=frozenset({"chat", "vision"}),
        regions=frozenset({"us"}),
    )

    with pytest.raises(ValueError, match="no statically eligible candidate"):
        RoutedBackplane(
            (capability_mismatch,),
            (
                policy(
                    "acme",
                    "capability-mismatch",
                    required_capabilities=frozenset({"vision"}),
                ),
            ),
        )
    with pytest.raises(ValueError, match="no statically eligible candidate"):
        RoutedBackplane((region_mismatch,), (policy("acme", "region-mismatch"),))


def test_candidate_name_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="candidate name"):
        candidate("", Backplane("provider", [[]]))


def test_policy_tenant_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="policy tenant"):
        policy("", "primary")


def test_policy_candidate_selection_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="needs candidates"):
        policy("acme")
    with pytest.raises(ValueError, match="candidate names"):
        policy("acme", "")


async def test_exact_candidate_model_override_shares_request_payload_and_preserves_events() -> None:
    event = ModelResponseEvent.text_delta("hello", request_id="provider-request-7")
    completed = ModelResponseEvent.completed(request_id="provider-request-7")
    plane = Backplane("provider", [[event, completed]])
    route = router(candidate("primary", plane, model="exact-model"))
    original = request()

    events = [item async for item in route.stream(original)]

    assert events == [event, completed]
    assert events[0] is event
    assert events[0].provider_request_id == "provider-request-7"
    routed = plane.requests[0]
    assert routed.model == "exact-model"
    assert routed.messages is original.messages
    assert routed.tools is original.tools
    assert routed.metadata is original.metadata
    assert (routed.max_output_tokens, routed.temperature) == (64, 0.2)


async def test_unhealthy_primary_is_skipped_without_invocation() -> None:
    primary = Backplane("primary", [[]])
    secondary = Backplane("secondary", [[ModelResponseEvent.text_delta("secondary")]])
    checks: list[str] = []

    def healthy(item: ModelCandidate) -> bool:
        checks.append(item.name)
        return item.name != "primary"

    route = router(candidate("primary", primary), candidate("secondary", secondary), health=healthy)

    events = [item async for item in route.stream(request())]

    assert [item.text for item in events] == ["secondary"]
    assert checks == ["primary", "secondary"]
    assert primary.requests == []
    assert len(secondary.requests) == 1


async def test_empty_primary_stream_falls_back_before_any_output() -> None:
    primary = Backplane("primary", [[]])
    completed = ModelResponseEvent.completed()
    secondary = Backplane("secondary", [[completed]])
    route = router(candidate("primary", primary), candidate("secondary", secondary))

    events = [item async for item in route.stream(request())]

    assert events == [completed]
    assert len(primary.requests) == len(secondary.requests) == 1


async def test_health_is_consulted_for_every_request_without_caching() -> None:
    primary = Backplane(
        "primary",
        [[ModelResponseEvent.text_delta("first")], [ModelResponseEvent.text_delta("second")]],
    )
    state = {"healthy": False}
    secondary = Backplane("secondary", [[ModelResponseEvent.text_delta("fallback")]])
    route = router(
        candidate("primary", primary),
        candidate("secondary", secondary),
        health=lambda item: state["healthy"] if item.name == "primary" else True,
    )

    first = [item.text async for item in route.stream(request())]
    state["healthy"] = True
    second = [item.text async for item in route.stream(request())]

    assert first == ["fallback"]
    assert second == ["first"]


async def test_retryable_failure_before_output_uses_next_eligible_candidate() -> None:
    failure = BackplaneError("busy", retryable=True, status=503, request_id="failed-request")
    primary = Backplane("primary", [failure])
    event = ModelResponseEvent.text_delta("recovered", request_id="recovered-request")
    secondary = Backplane("secondary", [[event]])
    route = router(candidate("primary", primary), candidate("secondary", secondary))

    events = [item async for item in route.stream(request())]

    assert events == [event]
    assert len(primary.requests) == len(secondary.requests) == 1


async def test_retryable_failure_after_text_never_falls_back() -> None:
    class Partial:
        name = "partial"

        async def stream(self, request: ModelRequest) -> Any:
            del request
            yield ModelResponseEvent.text_delta("started", request_id="request-1")
            raise BackplaneError(
                "disconnected",
                retryable=True,
                request_id="request-1",
                output_started=False,
            )

    secondary = Backplane("secondary", [[ModelResponseEvent.text_delta("wrong")]])
    route = router(
        candidate("primary", Partial()),
        candidate("secondary", secondary),
    )
    stream = route.stream(request())

    first = await anext(stream)
    assert (first.text, first.provider_request_id) == ("started", "request-1")
    with pytest.raises(BackplaneError) as caught:
        await anext(stream)
    assert caught.value.request_id == "request-1"
    assert secondary.requests == []


async def test_provider_output_started_signal_never_falls_back_before_an_event() -> None:
    failure = BackplaneError("started", retryable=True, output_started=True)
    primary = Backplane("primary", [failure])
    secondary = Backplane("secondary", [[ModelResponseEvent.text_delta("wrong")]])
    route = router(candidate("primary", primary), candidate("secondary", secondary))

    with pytest.raises(BackplaneError) as caught:
        _ = [item async for item in route.stream(request())]

    assert caught.value is failure
    assert secondary.requests == []


async def test_nonretryable_failure_never_falls_back() -> None:
    failure = BackplaneError("invalid", retryable=False, status=400, request_id="bad-request")
    primary = Backplane("primary", [failure])
    secondary = Backplane("secondary", [[ModelResponseEvent.text_delta("wrong")]])
    route = router(candidate("primary", primary), candidate("secondary", secondary))

    with pytest.raises(BackplaneError) as caught:
        _ = [item async for item in route.stream(request())]

    assert (caught.value.status, caught.value.request_id) == (400, "bad-request")
    assert secondary.requests == []


async def test_tenant_policy_never_borrows_another_tenants_candidate() -> None:
    acme = Backplane("acme", [[ModelResponseEvent.text_delta("acme")]])
    beta = Backplane("beta", [[ModelResponseEvent.text_delta("beta")]])
    route = router(
        candidate("acme-model", acme),
        candidate("beta-model", beta),
        policies=(
            policy("acme", "acme-model"),
            policy("beta", "beta-model"),
        ),
    )

    beta_events = [item.text async for item in route.stream(request("beta"))]

    assert beta_events == ["beta"]
    assert acme.requests == []
    with pytest.raises(BackplaneError, match="no model route policy for tenant 'unknown'"):
        _ = [item async for item in route.stream(request("unknown"))]


async def test_request_routing_metadata_is_snapshotted_at_construction() -> None:
    acme = Backplane("acme", [[ModelResponseEvent.text_delta("acme")]])
    beta = Backplane("beta", [[ModelResponseEvent.text_delta("beta")]])
    route = router(
        candidate("acme-model", acme),
        candidate("beta-model", beta),
        policies=(
            policy("acme", "acme-model"),
            policy("beta", "beta-model"),
        ),
    )
    metadata = {"tenant": "acme"}
    model_request = ModelRequest(
        "profile-placeholder",
        (ModelMessage("user", "hello"),),
        metadata=metadata,
    )

    metadata["tenant"] = "beta"
    events = [item.text async for item in route.stream(model_request)]

    assert events == ["acme"]
    assert beta.requests == []


@pytest.mark.parametrize(
    "required",
    [["vision"], {"vision": True}],
    ids=["list", "mapping"],
)
async def test_request_routing_fact_sequences_are_snapshotted_at_construction(
    required: Any,
) -> None:
    chat = Backplane("chat", [[ModelResponseEvent.text_delta("chat")]])
    vision = Backplane("vision", [[ModelResponseEvent.text_delta("vision")]])
    route = router(
        candidate("chat-model", chat, capabilities=frozenset({"chat"})),
        candidate("vision-model", vision, capabilities=frozenset({"chat", "vision"})),
    )
    model_request = ModelRequest(
        "profile-placeholder",
        (ModelMessage("user", "hello"),),
        metadata={"tenant": "acme", "required_capabilities": required},
    )

    required.clear()
    events = [item.text async for item in route.stream(model_request)]

    assert events == ["vision"]
    assert chat.requests == []


def test_model_request_refuses_non_mapping_metadata_at_construction() -> None:
    with pytest.raises(TypeError, match="metadata must be a mapping"):
        replace(request(), metadata=cast(Any, []))


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({}, "requires request.metadata\\['tenant'\\]"),
        ({"tenant": ""}, "requires request.metadata\\['tenant'\\]"),
        ({"tenant": 7}, "requires request.metadata\\['tenant'\\]"),
        (
            {"tenant": "acme", "required_capabilities": "vision"},
            "required_capabilities.*must contain strings",
        ),
        (
            {"tenant": "acme", "required_capabilities": 7},
            "required_capabilities.*must contain strings",
        ),
        (
            {"tenant": "acme", "required_capabilities": [""]},
            "required_capabilities.*must contain non-empty strings",
        ),
        (
            {"tenant": "acme", "allowed_regions": "au"},
            "allowed_regions.*must contain strings",
        ),
        (
            {"tenant": "acme", "allowed_regions": [7]},
            "allowed_regions.*must contain non-empty strings",
        ),
    ],
)
async def test_malformed_request_routing_facts_refuse(
    metadata: dict[str, Any],
    message: str,
) -> None:
    route = router(candidate("primary", Backplane("primary", [[]])))
    malformed = replace(request(), metadata=metadata)

    with pytest.raises(BackplaneError, match=message):
        _ = [item async for item in route.stream(malformed)]


async def test_request_tenant_must_not_be_empty() -> None:
    route = router(candidate("primary", Backplane("primary", [[]])))
    missing = replace(request(), metadata={"tenant": ""})

    with pytest.raises(BackplaneError, match="requires request.metadata"):
        _ = [item async for item in route.stream(missing)]


async def test_request_capability_and_residency_narrowing_never_crosses_requirements() -> None:
    au_chat = Backplane("au-chat", [[]])
    us_vision = Backplane("us-vision", [[]])
    route = router(
        candidate(
            "au-chat",
            au_chat,
            capabilities=frozenset({"chat", "tools"}),
            regions=frozenset({"au"}),
        ),
        candidate(
            "us-vision",
            us_vision,
            capabilities=frozenset({"chat", "vision"}),
            regions=frozenset({"us"}),
        ),
    )

    with pytest.raises(BackplaneError, match="no eligible model candidate"):
        _ = [
            item
            async for item in route.stream(
                request(
                    required_capabilities=frozenset({"vision"}),
                    allowed_regions=frozenset({"au"}),
                )
            )
        ]

    assert au_chat.requests == us_vision.requests == []


@pytest.mark.parametrize(
    ("required_capabilities", "allowed_regions"),
    [
        (frozenset({"vision"}), frozenset({"au"})),
        (frozenset({"chat"}), frozenset({"us"})),
    ],
)
async def test_request_capability_and_region_checks_each_refuse_crossing(
    required_capabilities: frozenset[str],
    allowed_regions: frozenset[str],
) -> None:
    plane = Backplane("au-chat", [[]])
    route = router(
        candidate(
            "au-chat",
            plane,
            capabilities=frozenset({"chat"}),
            regions=frozenset({"au"}),
        )
    )

    with pytest.raises(BackplaneError, match="no eligible model candidate"):
        _ = [
            item
            async for item in route.stream(
                request(
                    required_capabilities=required_capabilities,
                    allowed_regions=allowed_regions,
                )
            )
        ]

    assert plane.requests == []


async def test_all_request_eligible_candidates_unhealthy_is_retryable() -> None:
    plane = Backplane("primary", [[]])
    route = router(candidate("primary", plane), health=lambda _candidate: False)

    with pytest.raises(BackplaneError, match="no healthy model candidate") as caught:
        _ = [item async for item in route.stream(request())]

    assert caught.value.retryable is True
    assert plane.requests == []


async def test_all_healthy_but_failed_candidates_preserve_last_retryable_error() -> None:
    first = Backplane("first", [BackplaneError("first", retryable=True, request_id="one")])
    second_error = BackplaneError("second", retryable=True, request_id="two")
    second = Backplane("second", [second_error])
    route = router(candidate("first", first), candidate("second", second))

    with pytest.raises(BackplaneError) as caught:
        _ = [item async for item in route.stream(request())]

    assert caught.value is second_error
