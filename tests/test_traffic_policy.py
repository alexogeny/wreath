from __future__ import annotations

from types import SimpleNamespace

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies, EntityUid, authorize
from wreath.client_facts import (
    AgentFacts,
    ClientFacts,
    ClientFactsProvider,
    GeoIPRecord,
    IPFacts,
    UserAgentFacts,
)
from wreath.metrics import collect
from wreath.policy import (
    AIScrapingPolicy,
    HttpPolicy,
    TrafficClass,
    TrafficPolicy,
    traffic_class,
)
from wreath.policy.traffic import _compile_traffic_class
from wreath.signatures import robots_txt
from wreath.testing import TestClient


class VerifiedAgents:
    def facts(self, request):
        return SimpleNamespace(
            verified=True,
            agent="https://agent.example/directory",
        )


def _public_app(**options) -> Wreath:
    app = Wreath(**options)

    @app.get("/")
    async def home(request):
        return "ok"

    return app


@pytest.mark.asyncio
async def test_ai_scrapers_are_refused_by_default_but_user_fetchers_are_not() -> None:
    async with TestClient(_public_app()) as client:
        scraper = await client.get("/", headers={"user-agent": "ClaudeBot/1.0"})
        person = await client.get("/", headers={"user-agent": "Claude-User/1.0"})
        browser = await client.get("/", headers={"user-agent": "Mozilla/5.0"})
    assert scraper.status == 403
    assert scraper.json()["detail"] == "AI scraper traffic is disabled by default"
    assert person.status == 200
    assert browser.status == 200


@pytest.mark.asyncio
async def test_an_ai_scraper_cannot_hide_behind_another_bot_product() -> None:
    async with TestClient(_public_app()) as client:
        response = await client.get("/", headers={"user-agent": "Googlebot/1.0 GPTBot/1.0"})
    assert response.status == 403


@pytest.mark.asyncio
async def test_ai_scrapers_can_read_the_declaration_that_refuses_them() -> None:
    app = _public_app()

    @app.get("/robots.txt")
    async def robots(request):
        return robots_txt(app)

    async with TestClient(app) as client:
        response = await client.get("/robots.txt", headers={"user-agent": "GPTBot/1.0"})
    assert response.status == 200
    assert "User-agent: gptbot\n" in response.text
    assert "Disallow: /\n" in response.text


@pytest.mark.asyncio
async def test_an_application_can_opt_into_all_ai_scraping() -> None:
    async with TestClient(_public_app(ai_scraping="allow")) as client:
        response = await client.get("/", headers={"user-agent": "GPTBot/1.0"})
    assert response.status == 200


@pytest.mark.asyncio
async def test_an_application_can_allow_one_ai_scraper_and_refuse_the_rest() -> None:
    policy = HttpPolicy(ai_scraping=AIScrapingPolicy(allow=("gptbot",)))
    app = _public_app(http_policy=policy)
    async with TestClient(app) as client:
        openai = await client.get("/", headers={"user-agent": "GPTBot/1.0"})
        anthropic = await client.get("/", headers={"user-agent": "ClaudeBot/1.0"})
    assert openai.status == 200
    assert anthropic.status == 403
    readings = {(row.subsystem, row.instance): row.values for row in collect(app)}
    assert readings[("ai_scraping_policy", "default")] == {"refused": 1}


@pytest.mark.asyncio
async def test_an_explicit_ai_policy_replaces_the_injected_default() -> None:
    app = _public_app()
    app.configure_http_policy(HttpPolicy(ai_scraping=AIScrapingPolicy(allow=("gptbot",))))
    async with TestClient(app) as client:
        admitted = await client.get("/", headers={"user-agent": "GPTBot/1.0"})
        refused = await client.get("/", headers={"user-agent": "ClaudeBot/1.0"})
    assert admitted.status == 200
    assert refused.status == 403


def test_an_explicit_ai_policy_cannot_be_silently_replaced() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            ai_scraping=AIScrapingPolicy(allow=("gptbot",)),
        )
    )
    with pytest.raises(ValueError, match="ai_scraping.*already configured"):
        app.configure_http_policy(HttpPolicy(ai_scraping=AIScrapingPolicy(allow=("claudebot",))))


@pytest.mark.asyncio
async def test_an_admitted_ai_agent_can_then_be_verified_and_classified() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            ai_scraping=AIScrapingPolicy(allow=("gptbot",)),
        )
    )
    provider = ClientFactsProvider(signatures=VerifiedAgents())
    app.configure_http_policy(
        HttpPolicy(
            traffic=TrafficPolicy(
                provider,
                (
                    TrafficClass(
                        "verified-openai-agent",
                        claimed_agent=True,
                        verified_agent=True,
                        agent_identities=("https://agent.example/directory",),
                    ),
                ),
            )
        )
    )

    @app.get("/")
    async def home(request):
        return {"class": traffic_class(request)}

    async with TestClient(app) as client:
        response = await client.get("/", headers={"user-agent": "GPTBot/1.0"})
    assert response.status == 200
    assert response.json() == {"class": "verified-openai-agent"}


def test_ai_scraping_configuration_refuses_unknown_forms() -> None:
    with pytest.raises(ValueError, match="ai_scraping must be 'deny' or 'allow'"):
        Wreath(ai_scraping="sometimes")
    with pytest.raises(ValueError, match="unknown AI scraper allowance"):
        AIScrapingPolicy(allow=("mysterybot",))


def test_traffic_classes_refuse_empty_or_ambiguous_declarations() -> None:
    with pytest.raises(ValueError, match="at least one TrafficClass"):
        TrafficPolicy(ClientFactsProvider(), ())
    with pytest.raises(ValueError, match="has no match criteria"):
        TrafficClass("everything")
    declaration = TrafficClass("bot", claimed_agent=True)
    with pytest.raises(ValueError, match="duplicate traffic class"):
        TrafficPolicy(ClientFactsProvider(), (declaration, declaration))
    with pytest.raises(ValueError, match="default 'bot'.*duplicates a declared class"):
        TrafficPolicy(ClientFactsProvider(), (declaration,), default="bot")


def test_compiled_traffic_membership_matches_the_public_declaration() -> None:
    declaration = TrafficClass(
        "selected",
        countries=("AU", "NZ", "US", "GB"),
        browsers=("chrome", "firefox", "safari", "target"),
        agent_identities=("one", "two", "three", "agent"),
        claimed_agent=True,
        verified_agent=True,
        mobile=False,
    )
    compiled = _compile_traffic_class(declaration)
    cases = (
        ClientFacts(
            IPFacts("203.0.113.1", "peer", 4, True, False, False, GeoIPRecord("au")),
            UserAgentFacts("", browser="target", mobile=False),
            AgentFacts(claimed=True, verified=True, identity="agent"),
        ),
        ClientFacts(
            IPFacts("203.0.113.1", "peer", 4, True, False, False, GeoIPRecord("CA")),
            UserAgentFacts("", browser="target", mobile=False),
            AgentFacts(claimed=True, verified=True, identity="agent"),
        ),
        ClientFacts(
            None,
            UserAgentFacts("", browser="unknown", mobile=False),
            AgentFacts(claimed=True, verified=True, identity="agent"),
        ),
        ClientFacts(
            IPFacts("203.0.113.1", "peer", 4, True, False, False, GeoIPRecord("AU")),
            UserAgentFacts("", browser="target", mobile=False),
            AgentFacts(claimed=True, verified=True, identity="unknown"),
        ),
    )

    assert compiled is not declaration
    assert [compiled.matches(facts) for facts in cases] == [
        declaration.matches(facts) for facts in cases
    ]
    assert declaration.matches(cases[0]) is True
    assert declaration.matches(cases[1]) is False
    assert declaration.matches(cases[2]) is False


def test_a_class_without_country_rules_does_not_resolve_geography() -> None:
    class IPWithoutGeography:
        @property
        def geo(self):
            raise AssertionError("country-free traffic rules do not need geography")

    declaration = TrafficClass("claimed", claimed_agent=True)
    facts = ClientFacts(
        IPWithoutGeography(),
        UserAgentFacts(""),
        AgentFacts(claimed=True),
    )

    assert declaration.matches(facts)


def test_a_missing_country_does_not_match_a_country_rule() -> None:
    facts = ClientFacts(
        SimpleNamespace(geo=SimpleNamespace(country=None)),
        UserAgentFacts(""),
        AgentFacts(),
    )

    assert TrafficClass("australian", countries=("AU",)).matches(facts) is False


def test_traffic_compilation_preserves_subclass_match_overrides() -> None:
    class CustomTrafficClass(TrafficClass):
        def matches(self, facts):
            return True

    declaration = CustomTrafficClass(
        "custom",
        browsers=("one", "two", "three", "four"),
    )

    assert _compile_traffic_class(declaration) is declaration


def test_an_unactivated_request_has_the_default_traffic_class() -> None:
    assert traffic_class(SimpleNamespace()) == "unclassified"


@pytest.mark.asyncio
async def test_traffic_policy_classifies_before_the_handler_and_exports_counts() -> None:
    app = Wreath(ai_scraping="allow")
    provider = app.client_facts("public", geoip=None)
    policy = TrafficPolicy(
        provider,
        (
            TrafficClass("claimed-bot", claimed_agent=True, verified_agent=False),
            TrafficClass("mobile", mobile=True),
        ),
    )
    app.configure_http_policy(HttpPolicy(traffic=policy))

    @app.get("/class")
    async def selected(request):
        return {"class": traffic_class(request)}

    async with TestClient(app) as client:
        bot = await client.get("/class", headers={"user-agent": "ClaudeBot/1.0"})
        ordinary = await client.get("/class", headers={"user-agent": "curl/8.0"})

    assert bot.json() == {"class": "claimed-bot"}
    assert ordinary.json() == {"class": "unclassified"}
    readings = {(row.subsystem, row.instance): row.values for row in collect(app)}
    assert readings[("traffic_policy", "default")] == {
        "classified": 2,
        "matched": 1,
        "unmatched": 1,
        "denied": 0,
    }


@pytest.mark.asyncio
async def test_a_denied_class_never_reaches_the_handler() -> None:
    app = Wreath(ai_scraping="allow")
    provider = app.client_facts("public", geoip=None)
    app.configure_http_policy(
        HttpPolicy(
            traffic=TrafficPolicy(
                provider,
                (TrafficClass("unverified-bot", claimed_agent=True, deny=True),),
            )
        )
    )
    reached = False

    @app.get("/")
    async def home(request):
        nonlocal reached
        reached = True
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/", headers={"user-agent": "GPTBot/1.0"})
    assert response.status == 403
    assert response.json()["detail"] == "Client traffic policy refused this request"
    assert reached is False


@pytest.mark.asyncio
async def test_the_selected_class_is_in_the_default_cedar_context() -> None:
    app = Wreath()
    provider = ClientFactsProvider(signatures=VerifiedAgents())
    app.configure_http_policy(
        HttpPolicy(
            traffic=TrafficPolicy(
                provider,
                (TrafficClass("verified-agent", verified_agent=True),),
            )
        )
    )
    engine = CedarPolicies(
        'permit(principal, action == Action::"read", resource) '
        'when { context.client_class == "verified-agent" };'
    )

    async def verify(token: str) -> Identity:
        return Identity(token)

    app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

    @app.get("/")
    @authorize(action="read", resource=EntityUid("Document", "one"))
    async def home(request):
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/", headers={"authorization": "Bearer ada"})
    assert response.status == 200
