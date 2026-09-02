from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from importlib.resources import files
from types import SimpleNamespace

import pytest

from wreath import Wreath
from wreath.client_facts import (
    AgentFacts,
    ClientFacts,
    ClientFactsProvider,
    GeoIPRecord,
    IPFacts,
    UserAgentDatabase,
    UserAgentFacts,
    WreathGeoIP,
    client_fact_attributes,
)
from wreath.metrics import collect
from wreath.policy import AIScrapingPolicy, HttpPolicy
from wreath.policy.proxy import ProxyPolicy
from wreath.request import Request


def test_default_client_facts_does_not_import_the_resource_reader() -> None:
    code = (
        "import sys; from wreath import Wreath; app = Wreath(); "
        "assert app._user_agent_database.lookup('curl/8.10'); "
        "assert 'importlib.resources' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


async def receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


class Geo:
    def lookup(self, address: str) -> GeoIPRecord:
        assert address == "203.0.113.8"
        return GeoIPRecord(country="AU", asn=64500)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _range_records(ranges: list[tuple[int, int]]) -> bytes:
    encoded = bytearray()
    previous_end = -1
    for start, end in ranges:
        encoded.extend(_varint(start - previous_end - 1))
        encoded.extend(_varint(end - start))
        encoded.append(0)
        previous_end = end
    return bytes(encoded)


def test_client_facts_distinguish_socket_from_trusted_forwarding() -> None:
    request = Request(
        {
            "type": "http",
            "client": ("10.0.0.4", 1234),
            "headers": [
                (b"x-forwarded-for", b"203.0.113.8, 10.0.0.4"),
                (b"user-agent", b"Mozilla/5.0 Chrome/120 Linux"),
                (b"sec-ch-ua", b'"Chromium";v="120", "Not_A Brand";v="99"'),
                (b"sec-ch-ua-platform", b'"Linux"'),
                (b"sec-ch-ua-mobile", b"?0"),
            ],
        },
        receive,
    )
    before = ClientFactsProvider().resolve(request)
    assert before.ip is not None
    assert (before.ip.address, before.ip.source) == ("10.0.0.4", "socket")

    ProxyPolicy(trusted=["10.0.0.0/8"])._ingress_sync(request)
    after = ClientFactsProvider(Geo()).resolve(request)
    assert after.ip is not None
    assert (after.ip.address, after.ip.source, after.ip.geo) == (
        "203.0.113.8",
        "forwarded",
        GeoIPRecord(country="AU", asn=64500),
    )
    assert after.user_agent.browser == "Chromium"
    assert after.user_agent.browser_version == "120"
    assert after.user_agent.platform == "Linux"
    assert after.user_agent.mobile is False


def test_client_hints_skip_grease_brand_before_the_real_brand() -> None:
    request = Request(
        {
            "type": "http",
            "client": ("203.0.113.8", None),
            "headers": [
                (b"user-agent", b"Mozilla/5.0 Chrome/120 Linux"),
                (b"sec-ch-ua", b'"Not_A Brand";v="99", "Chromium";v="120"'),
            ],
        },
        receive,
    )

    facts = ClientFactsProvider().resolve(request)

    assert facts.user_agent.brands == (("Not_A Brand", "99"), ("Chromium", "120"))
    assert (facts.user_agent.browser, facts.user_agent.browser_version) == (
        "Chromium",
        "120",
    )


def test_client_hints_override_disagreeing_database_facts() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"user-agent", b"Mozilla/5.0 Chrome/120 Linux Mobile"),
                (b"sec-ch-ua", b'"Chromium";v="124"'),
                (b"sec-ch-ua-platform", b'"Windows"'),
                (b"sec-ch-ua-mobile", b"?0"),
            ],
        },
        receive,
    )

    database = SimpleNamespace(_classify=lambda raw: ("Chrome", "120", "Linux", True, False, 1))
    facts = ClientFactsProvider(user_agents=database).resolve(request)

    assert (
        facts.user_agent.browser,
        facts.user_agent.browser_version,
        facts.user_agent.platform,
        facts.user_agent.mobile,
    ) == ("Chromium", "124", "Windows", False)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"name": ""}, ValueError, "provider name must be non-empty"),
        ({"signatures": object()}, TypeError, "signatures must expose facts"),
    ],
)
def test_client_facts_provider_refuses_invalid_configuration(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        ClientFactsProvider(**kwargs)


def test_exact_builtin_geoip_reuses_the_provider_ip_parse(monkeypatch) -> None:
    geoip = WreathGeoIP()
    provider = ClientFactsProvider(geoip)

    def parsed_again(_self, _address):
        raise AssertionError("the exact built-in GeoIP provider parsed the address twice")

    monkeypatch.setattr(WreathGeoIP, "lookup", parsed_again)
    request = Request(
        {"type": "http", "client": ("4.1.1.1", None), "headers": []},
        receive,
    )

    facts = provider.resolve(request)

    assert facts.ip is not None
    assert facts.ip.geo == GeoIPRecord(country="US")


def test_client_facts_skip_only_an_explicitly_unarmed_flight_context() -> None:
    facts = ClientFacts(
        ip=None,
        user_agent=UserAgentFacts(raw="curl/8", browser="curl", browser_version="8", rule_id=7),
        agent=AgentFacts(claimed=False, verified=False),
    )

    class Context:
        def __init__(self, flight: int) -> None:
            self.flight = flight
            self.recorded: tuple[int, int, str] | None = None

        def _flight_client_facts(self, flags: int, rule_id: int, country: str) -> None:
            self.recorded = (flags, rule_id, country)

    off = Context(0)
    ClientFactsProvider._record_flight(SimpleNamespace(_context=off), facts)
    assert off.recorded is None

    armed = Context(1)
    ClientFactsProvider._record_flight(SimpleNamespace(_context=armed), facts)
    assert armed.recorded == (1, 7, "")


def test_client_facts_record_the_resolved_country_in_an_armed_flight() -> None:
    facts = ClientFacts(
        ip=IPFacts(
            address="203.0.113.8",
            source="forwarded",
            version=4,
            is_global=True,
            is_private=False,
            is_loopback=False,
            geo=GeoIPRecord(country="au"),
        ),
        user_agent=UserAgentFacts(raw="curl/8", rule_id=7),
        agent=AgentFacts(claimed=False, verified=False),
    )

    class Context:
        flight = 1
        recorded: tuple[int, int, str] | None = None

        def _flight_client_facts(self, flags: int, rule_id: int, country: str) -> None:
            self.recorded = (flags, rule_id, country)

    context = Context()
    ClientFactsProvider._record_flight(SimpleNamespace(_context=context), facts)

    assert context.recorded is not None
    flags, rule_id, country = context.recorded
    assert flags & (1 << 7)
    assert not flags & (1 << 8)
    assert rule_id == 7
    assert country == "AU"


def test_client_facts_record_socket_ipv6_without_a_forwarded_flag() -> None:
    facts = ClientFacts(
        ip=IPFacts(
            address="2001:db8::1",
            source="socket",
            version=6,
            is_global=False,
            is_private=True,
            is_loopback=False,
        ),
        user_agent=UserAgentFacts(raw="curl/8"),
    )

    class Context:
        flight = 1
        recorded: tuple[int, int, str] | None = None

        def _flight_client_facts(self, flags: int, rule_id: int, country: str) -> None:
            self.recorded = (flags, rule_id, country)

    context = Context()
    ClientFactsProvider._record_flight(SimpleNamespace(_context=context), facts)

    assert context.recorded is not None
    flags, _, _ = context.recorded
    assert flags & (1 << 5)
    assert not flags & (1 << 6)
    assert flags & (1 << 8)


def test_client_fact_metrics_are_fixed_aggregate_counts() -> None:
    request = Request(
        {
            "type": "http",
            "client": ("203.0.113.8", None),
            "headers": [(b"user-agent", b"ClaudeBot/1.0 Mobile")],
        },
        receive,
    )
    provider = ClientFactsProvider(Geo(), name="public")
    provider.resolve(request)
    reading = provider.counters()
    assert (reading.subsystem, reading.instance) == ("client_facts", "public")
    assert reading.values == {
        "resolutions": 1,
        "ip_known": 1,
        "ip_forwarded": 0,
        "ipv4": 1,
        "ipv6": 0,
        "geo_known": 1,
        "ua_known": 1,
        "bot": 1,
        "bot_verified": 0,
        "bot_claimed_verified": 0,
        "mobile_known": 0,
        "mobile": 0,
        "country_au": 1,
    }


@pytest.mark.parametrize(
    "classification",
    [
        ("Browser", None, None, None, False, 1),
        (None, None, "Platform", None, False, 1),
        (None, None, None, False, False, 1),
        (None, None, None, None, True, 1),
    ],
)
def test_each_user_agent_fact_independently_counts_as_known(classification) -> None:
    database = SimpleNamespace(_classify=lambda raw: classification)
    provider = ClientFactsProvider(user_agents=database)
    request = Request({"type": "http", "headers": []}, receive)

    provider.resolve(request)

    assert provider.counters().values["ua_known"] == 1


def test_client_fact_metrics_distinguish_ipv6_and_verified_humans() -> None:
    signatures = SimpleNamespace(
        facts=lambda request: SimpleNamespace(verified=True, agent="human-agent")
    )
    provider = ClientFactsProvider(signatures=signatures)
    request = Request({"type": "http", "client": ("2001:db8::1", None), "headers": []}, receive)

    provider.resolve(request)

    values = provider.counters().values
    assert (values["ipv4"], values["ipv6"]) == (0, 1)
    assert values["bot_verified"] == 1
    assert values["bot_claimed_verified"] == 0


def test_client_fact_attributes_follow_otel_names_without_identifiers() -> None:
    facts = ClientFacts(
        ip=IPFacts(
            address="203.0.113.8",
            source="forwarded",
            version=4,
            is_global=False,
            is_private=True,
            is_loopback=False,
            geo=GeoIPRecord(country="au", city="Brisbane", asn=64500),
        ),
        user_agent=UserAgentFacts(
            raw="ClaudeBot/1.0 private-fragment",
            browser="ClaudeBot",
            browser_version="1.0",
            platform="Linux",
            mobile=False,
            bot=True,
        ),
        agent=AgentFacts(
            claimed=True,
            verified=True,
            identity="https://agent.example/.well-known/bots.json",
        ),
    )
    attributes = client_fact_attributes(facts)
    assert attributes == {
        "user_agent.name": "ClaudeBot",
        "user_agent.version": "1.0",
        "user_agent.os.name": "Linux",
        "browser.mobile": False,
        "user_agent.synthetic.type": "bot",
        "wreath.client.agent.claimed": True,
        "wreath.client.agent.verified": True,
        "wreath.client.agent.identity": "https://agent.example/.well-known/bots.json",
        "network.type": "ipv4",
        "wreath.client.address_source": "forwarded",
        "geo.country.iso_code": "AU",
    }
    assert facts.ip.address not in attributes.values()
    assert facts.user_agent.raw not in attributes.values()


def test_client_fact_attributes_omit_an_oversized_browser_version() -> None:
    facts = ClientFacts(
        ip=None,
        user_agent=UserAgentFacts(raw="custom", browser_version="v" * 65),
        agent=AgentFacts(claimed=False, verified=False),
    )

    assert "user_agent.version" not in client_fact_attributes(facts)


def test_client_fact_attributes_omit_an_absent_browser_version() -> None:
    facts = ClientFacts(
        ip=None,
        user_agent=UserAgentFacts(raw="custom", browser_version=None),
        agent=AgentFacts(claimed=False, verified=False),
    )

    assert "user_agent.version" not in client_fact_attributes(facts)


def test_client_fact_attributes_omit_absent_and_untrusted_facts() -> None:
    empty = ClientFacts(ip=None, user_agent=UserAgentFacts(raw="custom"))
    assert client_fact_attributes(empty) == {}

    untrusted_ip = ClientFacts(
        ip=IPFacts(
            address="203.0.113.8",
            source="application",
            version=4,
            is_global=False,
            is_private=True,
            is_loopback=False,
            geo=GeoIPRecord(country="A1"),
        ),
        user_agent=UserAgentFacts(raw="custom"),
    )
    assert client_fact_attributes(untrusted_ip) == {"network.type": "ipv4"}


def test_client_fact_attributes_bound_client_controlled_values() -> None:
    facts = ClientFacts(
        ip=None,
        user_agent=UserAgentFacts(
            raw="r" * 1025,
            browser="b" * 129,
            platform="p" * 129,
        ),
        agent=AgentFacts(identity="i" * 513),
    )

    assert client_fact_attributes(facts, include_raw_user_agent=True) == {}

    permitted = ClientFacts(ip=None, user_agent=UserAgentFacts(raw="curl/8"))
    assert client_fact_attributes(permitted, include_raw_user_agent=True) == {
        "user_agent.original": "curl/8"
    }


@pytest.mark.parametrize(
    "agent",
    [AgentFacts(claimed=True), AgentFacts(verified=True)],
)
def test_client_fact_attributes_publish_independent_agent_states(agent) -> None:
    facts = ClientFacts(
        ip=None,
        user_agent=UserAgentFacts(raw="custom"),
        agent=agent,
    )

    assert client_fact_attributes(facts) == {
        "wreath.client.agent.claimed": agent.claimed,
        "wreath.client.agent.verified": agent.verified,
    }


def test_provider_caches_and_fuses_verified_agent_once_per_request() -> None:
    class Verified:
        calls = 0

        def facts(self, request: Request) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(
                verified=True,
                agent="https://claude.ai/.well-known/http-message-signatures-directory",
            )

    signatures = Verified()
    provider = ClientFactsProvider(signatures=signatures)
    request = Request(
        {
            "type": "http",
            "client": ("203.0.113.8", None),
            "headers": [(b"user-agent", b"ClaudeBot/1.0")],
        },
        receive,
    )
    first = provider.resolve(request)
    assert provider(request) is first
    assert signatures.calls == 1
    assert first.agent == AgentFacts(
        claimed=True,
        verified=True,
        identity="https://claude.ai/.well-known/http-message-signatures-directory",
    )
    assert provider.counters().values["resolutions"] == 1
    assert provider.counters().values["bot_claimed_verified"] == 1


@pytest.mark.parametrize(
    "signature_facts",
    [None, SimpleNamespace(verified=False, agent="untrusted-agent")],
)
def test_unverified_signature_facts_publish_no_identity(signature_facts) -> None:
    signatures = SimpleNamespace(facts=lambda request: signature_facts)
    request = Request({"type": "http", "headers": [(b"user-agent", b"curl/8")]}, receive)

    facts = ClientFactsProvider(signatures=signatures).resolve(request)

    assert facts.agent == AgentFacts(claimed=False, verified=False, identity=None)


def test_app_owns_client_facts_and_metrics_discovers_it() -> None:
    app = Wreath(ai_scraping="allow")
    provider = app.client_facts("public", geoip=None)
    assert app.state.client_facts_public is provider
    assert collect(app) == (provider.counters(), app.counters())
    with pytest.raises(ValueError, match="duplicate client-facts provider: public"):
        app.client_facts("public")


def test_default_ai_policy_and_client_facts_share_one_user_agent_database() -> None:
    app = Wreath()
    provider = app.client_facts("public", geoip=None)
    assert app._http_policy.ai_scraping._database is provider._user_agents


def test_explicit_ai_policy_and_client_facts_share_one_user_agent_database() -> None:
    policy = AIScrapingPolicy(allow=("gptbot",))
    app = Wreath(http_policy=HttpPolicy(ai_scraping=policy))
    provider = app.client_facts("public", geoip=None)
    assert policy._database is provider._user_agents


@pytest.mark.parametrize(
    "headers",
    (
        [],
        [(b"host", b"example.com"), (b"user-agent", b"Mozilla/5.0")],
        [(b"user-agent", b"GPTBot/1.0"), (b"accept", b"*/*")],
        [[b"host", b"example.com"], [b"user-agent", b"ClaudeBot/1.0"]],
    ),
)
def test_native_ai_header_scan_matches_the_independent_two_step_path(headers) -> None:
    policy = AIScrapingPolicy()
    database = policy._database._database
    user_agent = next(
        (value for name, value in headers if name == b"user-agent"),
        b"",
    )
    expected = database.blocked(user_agent, policy._blocked_table)
    assert database.blocked_headers(headers, policy._blocked_table) is expected


def test_native_user_agent_database_scans_products_without_regexes(tmp_path) -> None:
    database = UserAgentDatabase()
    browser, version, platform, mobile, bot = database.lookup(
        "Mozilla/5.0 (Linux; Android 14) Chrome/124.0 Mobile Safari/537.36"
    )
    assert (browser, version, platform, mobile, bot) == (
        "Chrome",
        "124.0",
        "Android",
        True,
        False,
    )
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "token": "wreathprobe",
                        "browser": "Wreath Probe",
                        "bot": True,
                        "priority": 100,
                    }
                ]
            }
        )
    )
    custom = UserAgentDatabase.from_path(path)
    assert custom.lookup("wreathprobe/2.1") == (
        "Wreath Probe",
        "2.1",
        None,
        None,
        True,
    )


def test_user_agent_database_loads_its_binary_form_from_a_path(tmp_path) -> None:
    image = files("wreath").joinpath("_data", "user_agent.wua").read_bytes()
    path = tmp_path / "agents.wua"
    path.write_bytes(image)

    database = UserAgentDatabase.from_path(path)

    assert database.lookup("Googlebot/1.0")[4] is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "needs an 'entries' list"),
        ({}, "needs an 'entries' list"),
        ({"entries": {}}, "needs an 'entries' list"),
        ({"entries": ["invalid"]}, "entry 0 must be an object"),
        ({"entries": [{}]}, "entry 0 needs string 'token'"),
    ],
)
def test_user_agent_database_refuses_malformed_json(tmp_path, payload, message) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        UserAgentDatabase.from_path(path)


def test_bundled_user_agent_database_is_real_bounded_data() -> None:
    image = files("wreath").joinpath("_data", "user_agent.wua").read_bytes()
    assert image.startswith(b"WUA1")
    assert len(image) <= 5_000
    database = UserAgentDatabase()
    assert database.lookup("Googlebot-Image/1.0") == (
        "Googlebot",
        "1.0",
        None,
        None,
        True,
    )
    assert database.lookup("Mozilla/5.0 HeadlessChrome/124.0 Linux") == (
        "Headless Chrome",
        "124.0",
        "Linux",
        False,
        True,
    )


@pytest.mark.parametrize(
    ("user_agent", "client"),
    [
        (
            "Mozilla/5.0 Chrome/131.0; compatible; OAI-SearchBot/1.4",
            "OpenAI SearchBot",
        ),
        ("Claude-SearchBot/1.0", "Claude SearchBot"),
        ("Perplexity-User/1.0", "Perplexity User"),
        ("Meta-ExternalAgent/1.1", "Meta External Agent"),
        ("Bytespider/1.0", "Bytespider"),
        ("Scrapy/2.13", "Scrapy"),
    ],
)
def test_bundled_user_agent_database_covers_contemporary_automation(
    user_agent: str, client: str
) -> None:
    browser, _, _, _, bot = UserAgentDatabase().lookup(user_agent)
    assert (browser, bot) == (client, True)


@pytest.mark.parametrize(
    ("user_agent", "client"),
    [
        ("python-requests/2.32.4", "Python Requests"),
        ("aws-sdk-go-v2/1.38.0", "AWS SDK for Go"),
        ("Mozilla/5.0 HuaweiBrowser/15.0 Mobile", "Huawei Browser"),
    ],
)
def test_bundled_user_agent_database_covers_clients_without_calling_them_bots(
    user_agent: str, client: str
) -> None:
    browser, _, _, _, bot = UserAgentDatabase().lookup(user_agent)
    assert (browser, bot) == (client, False)


def test_bundled_geoip_database_is_exact_bounded_ipv4_and_ipv6_data() -> None:
    image = files("wreath").joinpath("_data", "country.wgd").read_bytes()
    assert image.startswith(b"WGD2")
    assert len(image) <= 20_000
    database = WreathGeoIP()
    assert database.lookup("4.1.1.1") == GeoIPRecord(country="US")
    assert database.lookup("2001:8000::1") == GeoIPRecord(country="AU")
    assert database.lookup("127.0.0.1") is None


def test_wreath_geoip_uses_exact_ranges_across_lookup_directories(tmp_path) -> None:
    v4 = [
        (int(ipaddress.IPv4Address("1.255.255.250")), int(ipaddress.IPv4Address("2.0.0.5"))),
        (int(ipaddress.IPv4Address("203.0.113.0")), int(ipaddress.IPv4Address("203.0.113.255"))),
    ]
    first_v6 = int(ipaddress.IPv6Address("2001:db8::")) >> 64
    image = (
        b"WGD2\x01\x02\x00\x01\x00AU" + _range_records(v4) + _range_records([(first_v6, first_v6)])
    )
    path = tmp_path / "exact.wgd"
    path.write_bytes(image)
    database = WreathGeoIP(path)

    assert database.lookup("1.255.255.249") is None
    assert database.lookup("1.255.255.250") == GeoIPRecord(country="AU")
    assert database.lookup("2.0.0.5") == GeoIPRecord(country="AU")
    assert database.lookup("2.0.0.6") is None
    assert database.lookup("2001:db8::dead") == GeoIPRecord(country="AU")
    assert database.lookup("2001:db9::") is None


def test_oversized_user_agent_remains_a_raw_unclassified_fact() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"user-agent", b"x" * 8193)],
        },
        receive,
    )
    facts = ClientFactsProvider().resolve(request).user_agent
    assert facts.raw == "x" * 8193
    assert (facts.browser, facts.platform, facts.mobile, facts.bot) == (
        None,
        None,
        None,
        False,
    )


def test_wreath_geoip_refuses_an_oversized_database(tmp_path) -> None:
    path = tmp_path / "oversized.wgd"
    path.write_bytes(b"WGD2" + bytes(19_997))
    with pytest.raises(ValueError, match="9..20000-byte WGD2"):
        WreathGeoIP(path)
