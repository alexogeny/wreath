from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wreath.client_facts import (
    AgentFacts,
    ClientFacts,
    ClientFactsProvider,
    GeoIPRecord,
    IPFacts,
    UserAgentFacts,
)
from wreath.policy.traffic import (
    AIScrapingPolicy,
    TrafficClass,
    TrafficPolicy,
    _membership,
    _record_ai_refusal,
)


def _facts(
    *,
    ip: IPFacts | None = None,
    browser: str | None = None,
    mobile: bool | None = None,
    claimed: bool = False,
    verified: bool = False,
    identity: str | None = None,
) -> ClientFacts:
    return ClientFacts(
        ip,
        UserAgentFacts("", browser=browser, mobile=mobile),
        AgentFacts(claimed=claimed, verified=verified, identity=identity),
    )


def _ip(
    *,
    version: int = 4,
    source: str = "socket",
    country: str | None = "AU",
) -> IPFacts:
    return IPFacts("203.0.113.1", source, version, True, False, False, GeoIPRecord(country))


def test_ai_refusal_calls_the_flight_recorder_when_present() -> None:
    flags: list[int] = []
    target = SimpleNamespace(_context=SimpleNamespace(_flight_policy_refusal=flags.append))

    _record_ai_refusal(target)

    assert len(flags) == 1
    assert flags[0] != 0


def test_membership_compilation_preserves_small_sequences_and_hashes_large_ones() -> None:
    small = ("one", "two", "three")
    large = (*small, "four")

    assert _membership(small) is small
    assert _membership(large) == frozenset(large)
    assert isinstance(_membership(large), frozenset)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_ai_scope_admits_robots_reads_without_header_work(method: str) -> None:
    policy = AIScrapingPolicy()
    scope = SimpleNamespace(
        headers=pytest.fail,
        _flight_policy_refusal=pytest.fail,
    )

    assert policy._ingress_scope(scope, method, "/robots.txt") is None


def test_ai_scope_does_not_exempt_non_read_robots_requests() -> None:
    policy = AIScrapingPolicy()
    scope = {"headers": [(b"user-agent", b"GPTBot/1.0")]}

    response = policy._ingress_scope(scope, "POST", "/robots.txt")

    assert response is not None
    assert response.status == 403


def test_ai_scope_with_no_blocked_products_does_no_header_work() -> None:
    policy = AIScrapingPolicy(allow=True)
    scope = SimpleNamespace(headers=pytest.fail)

    assert policy._ingress_scope(scope, "POST", "/") is None


def test_ai_policy_refuses_an_allowance_container_other_than_a_tuple() -> None:
    allowance: Any = ["gptbot"]

    with pytest.raises(TypeError, match="must be bool or a tuple"):
        AIScrapingPolicy(allow=allowance)


def test_ai_policy_uses_an_injected_user_agent_database() -> None:
    class Database:
        def __init__(self) -> None:
            self.products: list[str] = []
            self._database = SimpleNamespace()

        def _classify(self, product: str) -> tuple[None, None, None, None, bool, int]:
            self.products.append(product)
            return None, None, None, None, False, len(self.products)

    database: Any = Database()

    policy = AIScrapingPolicy(_database=database)

    assert database.products == list(policy.blocked_products)


def test_ai_policy_refuses_a_missing_bundled_scraper_rule() -> None:
    database: Any = SimpleNamespace(
        _classify=lambda _product: (None, None, None, None, False, 0),
        _database=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="has no AI scraper rule"):
        AIScrapingPolicy(_database=database)


def test_ai_policy_blocked_products_exclude_explicit_allowances() -> None:
    policy = AIScrapingPolicy(allow=("gptbot",))

    assert "gptbot" not in policy.blocked_products
    assert "claudebot" in policy.blocked_products


def test_ai_sync_ingress_does_not_exempt_non_robots_reads() -> None:
    request = SimpleNamespace(
        path="/",
        method="GET",
        header=lambda *_args: "GPTBot/1.0",
    )

    response = AIScrapingPolicy()._ingress_sync(request)

    assert response is not None
    assert response.status == 403


def test_ai_scope_reads_headers_from_object_scopes() -> None:
    scope = SimpleNamespace(headers=[(b"user-agent", b"GPTBot/1.0")])

    response = AIScrapingPolicy()._ingress_scope(scope, "GET", "/")

    assert response is not None
    assert response.status == 403


@pytest.mark.parametrize("name", ["", "café"])
def test_traffic_class_name_must_be_non_empty_ascii(name: str) -> None:
    with pytest.raises(ValueError, match="name must be non-empty printable ASCII"):
        TrafficClass(name, claimed_agent=True)


@pytest.mark.parametrize("country", ["A", "AUS", "A1", "au", "ÅU"])
def test_traffic_class_country_must_be_an_uppercase_ascii_pair(country: str) -> None:
    with pytest.raises(ValueError, match="uppercase two-letter ISO code"):
        TrafficClass("country", countries=(country,))


@pytest.mark.parametrize("version", [True, False, 0, 5, "4"])
def test_traffic_class_ip_version_must_be_four_or_six(version: Any) -> None:
    with pytest.raises(ValueError, match="only 4 and 6"):
        TrafficClass("version", ip_versions=(version,))


@pytest.mark.parametrize("source", ["", "peer", "Forwarded"])
def test_traffic_class_address_source_uses_the_bounded_vocabulary(source: str) -> None:
    with pytest.raises(ValueError, match="must be 'socket' or 'forwarded'"):
        TrafficClass("source", address_sources=(source,))


@pytest.mark.parametrize(
    "field",
    [
        "countries",
        "browsers",
        "ip_versions",
        "address_sources",
        "agent_identities",
    ],
)
def test_traffic_class_match_collections_must_be_immutable_tuples(field: str) -> None:
    values: dict[str, Any] = {
        "countries": ["AU"],
        "browsers": ["firefox"],
        "ip_versions": [4],
        "address_sources": ["socket"],
        "agent_identities": ["trusted"],
    }

    with pytest.raises(TypeError, match=rf"{field} must be a tuple"):
        TrafficClass("mutable", **{field: values[field]})


@pytest.mark.parametrize("field", ["claimed_agent", "verified_agent", "mobile"])
@pytest.mark.parametrize("value", [0, 1, "false"])
def test_traffic_class_optional_flags_must_be_exact_booleans(
    field: str, value: Any
) -> None:
    with pytest.raises(TypeError, match=rf"{field} must be bool or None"):
        TrafficClass("flag", countries=("AU",), **{field: value})


@pytest.mark.parametrize("value", [0, 1, "false"])
def test_traffic_class_deny_must_be_an_exact_boolean(value: Any) -> None:
    with pytest.raises(TypeError, match="deny must be a bool"):
        TrafficClass("deny", countries=("AU",), deny=value)


@pytest.mark.parametrize("version", [4.0, 6.0])
def test_traffic_class_ip_versions_must_be_exact_integers(version: float) -> None:
    with pytest.raises(ValueError, match="only 4 and 6 as integers"):
        TrafficClass("version", ip_versions=(version,))


@pytest.mark.parametrize("name", ["line\nfeed", "carriage\rreturn", "nul\0byte"])
def test_traffic_class_name_must_not_contain_control_characters(name: str) -> None:
    with pytest.raises(ValueError, match="printable ASCII"):
        TrafficClass(name, claimed_agent=True)


@pytest.mark.parametrize("field", ["browsers", "agent_identities"])
def test_traffic_class_string_criteria_refuse_control_characters(field: str) -> None:
    with pytest.raises(ValueError, match="without control characters"):
        TrafficClass("controlled", **{field: ("value\n",)})


def test_browser_condition_can_independently_refuse_an_otherwise_matching_request() -> None:
    rule = TrafficClass("browser", browsers=("firefox",), claimed_agent=True)

    assert rule.matches(_facts(browser="firefox", claimed=True)) is True
    assert rule.matches(_facts(browser="chrome", claimed=True)) is False


def test_ip_version_condition_requires_an_ip_and_the_declared_version() -> None:
    rule = TrafficClass("ipv6", ip_versions=(6,), claimed_agent=True)

    assert rule.matches(_facts(ip=None, claimed=True)) is False
    assert rule.matches(_facts(ip=_ip(version=4), claimed=True)) is False
    assert rule.matches(_facts(ip=_ip(version=6), claimed=True)) is True


def test_address_source_condition_requires_an_ip_and_the_declared_source() -> None:
    rule = TrafficClass("forwarded", address_sources=("forwarded",), claimed_agent=True)

    assert rule.matches(_facts(ip=None, claimed=True)) is False
    assert rule.matches(_facts(ip=_ip(source="socket"), claimed=True)) is False
    assert rule.matches(_facts(ip=_ip(source="forwarded"), claimed=True)) is True


def test_agent_identity_requires_both_verification_and_membership() -> None:
    rule = TrafficClass("agent", agent_identities=("trusted",), claimed_agent=True)

    assert rule.matches(_facts(claimed=True, verified=False, identity="trusted")) is False
    assert rule.matches(_facts(claimed=True, verified=True, identity="other")) is False
    assert rule.matches(_facts(claimed=True, verified=True, identity="trusted")) is True


def test_verified_agent_condition_can_independently_refuse_a_match() -> None:
    rule = TrafficClass("verified", claimed_agent=True, verified_agent=True)

    assert rule.matches(_facts(claimed=True, verified=False)) is False
    assert rule.matches(_facts(claimed=True, verified=True)) is True


def test_mobile_condition_distinguishes_unset_false_and_true() -> None:
    rule = TrafficClass("desktop", claimed_agent=True, mobile=False)

    assert rule.matches(_facts(claimed=True, mobile=None)) is False
    assert rule.matches(_facts(claimed=True, mobile=True)) is False
    assert rule.matches(_facts(claimed=True, mobile=False)) is True


@pytest.mark.parametrize("mobile", [None, False, True])
def test_an_unspecified_mobile_condition_accepts_every_mobile_fact(mobile: bool | None) -> None:
    rule = TrafficClass("claimed", claimed_agent=True)

    assert rule.matches(_facts(claimed=True, mobile=mobile)) is True


def test_traffic_policy_refuses_a_non_provider() -> None:
    provider: Any = object()

    with pytest.raises(TypeError, match="must be a ClientFactsProvider"):
        TrafficPolicy(provider, (TrafficClass("bot", claimed_agent=True),))


def test_traffic_policy_classes_must_be_an_immutable_tuple() -> None:
    classes: Any = [TrafficClass("bot", claimed_agent=True)]

    with pytest.raises(TypeError, match="classes must be a tuple"):
        TrafficPolicy(ClientFactsProvider(), classes)


def test_traffic_policy_names_a_non_traffic_class_declaration() -> None:
    declaration: Any = object()

    with pytest.raises(TypeError, match=r"classes\[0\] must be a TrafficClass"):
        TrafficPolicy(ClientFactsProvider(), (declaration,))


@pytest.mark.parametrize("default", ["", "café", "line\nfeed"])
def test_traffic_policy_default_must_be_non_empty_ascii(default: str) -> None:
    with pytest.raises(ValueError, match="default class must be non-empty printable ASCII"):
        TrafficPolicy(
            ClientFactsProvider(),
            (TrafficClass("bot", claimed_agent=True),),
            default=default,
        )
