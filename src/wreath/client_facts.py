"""Trust-aware client IP, User-Agent, Client Hints, and optional GeoIP facts.

The address always comes from `Request.client`.  That is the socket peer unless
`ProxyPolicy` accepted a configured proxy and rewrote it, so this module never
interprets a forwarding header on its own. Browser and platform values remain
explicitly client-asserted facts; they are useful for presentation and analytics,
not authorization.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ._native import _core
from ._reqcache import resolve_keyed_once
from .request import Request

if TYPE_CHECKING:
    from .metrics import Counters

__all__ = [
    "AgentFacts",
    "ClientFacts",
    "ClientFactsProvider",
    "GeoIPProvider",
    "GeoIPRecord",
    "IPFacts",
    "UserAgentDatabase",
    "UserAgentFacts",
    "WreathGeoIP",
    "client_fact_attributes",
]

_BRAND = re.compile(r'"([^"\\]{1,128})"\s*;\s*v="([^"\\]{1,32})"')


def _builtin_database(name: str) -> bytes:
    return files("wreath").joinpath("_data", name).read_bytes()


@dataclass(frozen=True, slots=True)
class UserAgentFacts:
    raw: str
    brands: tuple[tuple[str, str], ...] = ()
    browser: str | None = None
    browser_version: str | None = None
    platform: str | None = None
    mobile: bool | None = None
    bot: bool = False
    rule_id: int = 0


@dataclass(frozen=True, slots=True)
class AgentFacts:
    """Claimed automation fused with cryptographic Web Bot Auth identity.

    A User-Agent can only claim that it is automated. `verified` means the
    request signature was accepted by the configured first-party signature
    layer; `identity` is published only for that verified result.
    """

    claimed: bool = False
    verified: bool = False
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class GeoIPRecord:
    country: str | None = None
    subdivision: str | None = None
    city: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    asn: int | None = None
    organization: str | None = None


@runtime_checkable
class GeoIPProvider(Protocol):
    def lookup(self, address: str) -> GeoIPRecord | None: ...


@dataclass(frozen=True, slots=True)
class IPFacts:
    address: str
    source: str
    version: int
    is_global: bool
    is_private: bool
    is_loopback: bool
    geo: GeoIPRecord | None = None


@dataclass(frozen=True, slots=True)
class ClientFacts:
    ip: IPFacts | None
    user_agent: UserAgentFacts
    agent: AgentFacts = AgentFacts()


class ClientFactsProvider:
    """Resolve request facts through optional declarative lookup providers.

    The provider owns a small fixed set of aggregate counters. They expose
    lookup coverage and coarse traffic shape without exporting an address,
    User-Agent, country, browser, or platform as a metric label. In particular,
    Client Hints are untrusted strings and must not be allowed to create an
    unbounded Prometheus or DogStatsD series set.
    """

    __slots__ = (
        "_counts",
        "_geoip",
        "_lock",
        "_name",
        "_signatures",
        "_user_agents",
    )

    def __init__(
        self,
        geoip: GeoIPProvider | None = None,
        user_agents: UserAgentDatabase | None = None,
        *,
        name: str = "default",
        signatures: Any = None,
    ) -> None:
        if not name:
            raise ValueError("client-facts provider name must be non-empty")
        self._geoip = geoip
        self._name = name
        if signatures is not None and not callable(getattr(signatures, "facts", None)):
            raise TypeError("client-facts signatures must expose facts(request)")
        self._signatures = signatures
        self._user_agents = user_agents or UserAgentDatabase()
        self._lock = threading.Lock()
        self._counts = {
            "resolutions": 0,
            "ip_known": 0,
            "ip_forwarded": 0,
            "ipv4": 0,
            "ipv6": 0,
            "geo_known": 0,
            "ua_known": 0,
            "bot": 0,
            "bot_verified": 0,
            "bot_claimed_verified": 0,
            "mobile_known": 0,
            "mobile": 0,
        }

    def resolve(self, request: Request) -> ClientFacts:
        return resolve_keyed_once(
            request,
            "_client_facts",
            self,
            lambda: self._resolve_uncached(request),
        )

    def _resolve_uncached(self, request: Request) -> ClientFacts:
        """Resolve one provider after the request-cache miss is established."""
        raw = request.header("user-agent", "") or ""
        brands = tuple(_BRAND.findall(request.header("sec-ch-ua", "") or ""))
        try:
            db_browser, db_version, db_platform, db_mobile, db_bot, rule_id = (
                self._user_agents._classify(raw)
            )
        except ValueError:
            # A header larger than the native database's declared boundary is
            # unclassified input, not an application error. Keep the raw fact
            # visible while declining to infer product facts from it.
            db_browser = db_version = db_platform = None
            db_mobile = None
            db_bot = False
            rule_id = 0
        selected_brand = next(
            ((brand, version) for brand, version in brands
             if "not" not in brand.lower()),
            None,
        )
        browser = db_browser if selected_brand is None else selected_brand[0]
        browser_version = db_version if selected_brand is None else selected_brand[1]
        platform_hint = request.header("sec-ch-ua-platform")
        platform = (
            platform_hint.strip().removeprefix('"').removesuffix('"')
            if platform_hint else db_platform
        )
        mobile_hint = request.header("sec-ch-ua-mobile")
        mobile = db_mobile if mobile_hint is None else mobile_hint.strip() == "?1"
        ua = UserAgentFacts(
            raw=raw,
            brands=brands,
            browser=browser,
            browser_version=browser_version,
            platform=platform,
            mobile=mobile,
            bot=db_bot,
            rule_id=rule_id,
        )
        peer = request.client
        ip = None
        if peer is not None:
            try:
                parsed = ipaddress.ip_address(peer[0])
            except ValueError:
                pass
            else:
                address = str(parsed)
                ip = IPFacts(
                    address=address,
                    source=request.client_source,
                    version=parsed.version,
                    is_global=parsed.is_global,
                    is_private=parsed.is_private,
                    is_loopback=parsed.is_loopback,
                    geo=None if self._geoip is None else self._geoip.lookup(address),
                )
        signature_facts = (
            None if self._signatures is None else self._signatures.facts(request)
        )
        verified = bool(
            signature_facts is not None
            and getattr(signature_facts, "verified", False)
        )
        identity = (
            getattr(signature_facts, "agent", None) if verified else None
        )
        agent = AgentFacts(claimed=ua.bot, verified=verified, identity=identity)
        facts = ClientFacts(ip=ip, user_agent=ua, agent=agent)
        self._record_flight(request, facts)
        with self._lock:
            counts = self._counts
            counts["resolutions"] += 1
            counts["ua_known"] += int(
                ua.browser is not None
                or ua.platform is not None
                or ua.mobile is not None
                or ua.bot
            )
            counts["bot"] += int(ua.bot)
            counts["bot_verified"] += int(agent.verified)
            counts["bot_claimed_verified"] += int(agent.claimed and agent.verified)
            counts["mobile_known"] += int(ua.mobile is not None)
            counts["mobile"] += int(ua.mobile is True)
            if ip is not None:
                counts["ip_known"] += 1
                counts["ip_forwarded"] += int(ip.source == "forwarded")
                counts["ipv4" if ip.version == 4 else "ipv6"] += 1
                counts["geo_known"] += int(ip.geo is not None)
                country = _country_code(ip.geo)
                if country is not None:
                    key = f"country_{country.lower()}"
                    counts[key] = counts.get(key, 0) + 1
        return facts

    def __call__(self, request: Request) -> ClientFacts:
        """Resolve this provider as a dependency, once for this request."""
        return self.resolve(request)

    @staticmethod
    def _record_flight(request: Request, facts: ClientFacts) -> None:
        context = getattr(request, "_context", None)
        record = getattr(context, "_flight_client_facts", None)
        if not callable(record):
            return
        ua = facts.user_agent
        ip = facts.ip
        flags = 0
        flags |= int(ua.rule_id != 0) << 0
        flags |= int(facts.agent.claimed) << 1
        flags |= int(facts.agent.verified) << 2
        flags |= int(ua.mobile is not None) << 3
        flags |= int(ua.mobile is True) << 4
        flags |= int(ip is not None) << 5
        flags |= int(ip is not None and ip.source == "forwarded") << 6
        country = None if ip is None else _country_code(ip.geo)
        flags |= int(country is not None) << 7
        flags |= int(ip is not None and ip.version == 6) << 8
        record(flags, ua.rule_id, "" if country is None else country)

    def counters(self) -> Counters:
        """Bounded aggregate readings for every first-party metrics bridge."""
        from .metrics import Counters

        with self._lock:
            values = dict(self._counts)
        return Counters("client_facts", self._name, values)


def client_fact_attributes(
    facts: ClientFacts,
    *,
    include_raw_user_agent: bool = False,
) -> dict[str, str | bool]:
    """Privacy-conscious OpenTelemetry-compatible event attributes.

    The default omits the IP address and original User-Agent. Country is the
    coarsest useful location, and malformed values from a pluggable database are
    omitted. Browser, version, and platform are bounded before crossing an
    exporter boundary because Client Hints can override database values.

    Set `include_raw_user_agent=True` only when the application's telemetry
    policy explicitly permits the potentially identifying header value.
    """
    attributes: dict[str, str | bool] = {}
    ua = facts.user_agent
    if ua.browser is not None and len(ua.browser) <= 128:
        attributes["user_agent.name"] = ua.browser
    if ua.browser_version is not None and len(ua.browser_version) <= 64:
        attributes["user_agent.version"] = ua.browser_version
    if ua.platform is not None and len(ua.platform) <= 128:
        attributes["user_agent.os.name"] = ua.platform
    if ua.mobile is not None:
        attributes["browser.mobile"] = ua.mobile
    if ua.bot:
        attributes["user_agent.synthetic.type"] = "bot"
    if facts.agent.claimed or facts.agent.verified:
        attributes["wreath.client.agent.claimed"] = facts.agent.claimed
        attributes["wreath.client.agent.verified"] = facts.agent.verified
    if facts.agent.identity is not None and len(facts.agent.identity) <= 512:
        attributes["wreath.client.agent.identity"] = facts.agent.identity
    if include_raw_user_agent and len(ua.raw) <= 1024:
        attributes["user_agent.original"] = ua.raw

    ip = facts.ip
    if ip is not None:
        attributes["network.type"] = f"ipv{ip.version}"
        if ip.source in {"socket", "forwarded"}:
            attributes["wreath.client.address_source"] = ip.source
        country = _country_code(ip.geo)
        if country is not None:
            attributes["geo.country.iso_code"] = country
    return attributes


def _country_code(record: GeoIPRecord | None) -> str | None:
    country = None if record is None else record.country
    if (
        country is None
        or len(country) != 2
        or not country.isascii()
        or not country.isalpha()
    ):
        return None
    return country.upper()


class UserAgentDatabase:
    """Native-owned, single-scan product-token database for User-Agent facts.

    The default database covers common browsers, platforms, command-line
    clients, and crawlers from Wreath's bundled WUA1 database. `from_path`
    loads either another WUA1 image or the documented JSON form. The compact
    image is copied once into operation-owned native storage and indexed there,
    so lookup never iterates Python rules or runs one regex per known client.
    """

    __slots__ = ("_database",)

    def __init__(self, entries: Any = None) -> None:
        source = _builtin_database("user_agent.wua") if entries is None else entries
        self._database = _core.UserAgentDB(source)

    @classmethod
    def from_path(cls, path: str | Path) -> UserAgentDatabase:
        raw = Path(path).read_bytes()
        if raw.startswith(b"WUA1"):
            return cls(raw)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise ValueError("UA database JSON needs an 'entries' list")
        entries = []
        for index, row in enumerate(payload["entries"]):
            if not isinstance(row, dict):
                raise ValueError(f"UA database entry {index} must be an object")
            try:
                entries.append((
                    row["token"], row.get("browser"), row.get("platform"),
                    row.get("mobile", -1), row.get("bot", False),
                    row.get("priority", 0),
                ))
            except KeyError as exc:
                raise ValueError(
                    f"UA database entry {index} needs string 'token'"
                ) from exc
        return cls(entries)

    def lookup(
        self, user_agent: str
    ) -> tuple[str | None, str | None, str | None, bool | None, bool]:
        return self._classify(user_agent)[:5]

    def _classify(
        self, user_agent: str
    ) -> tuple[str | None, str | None, str | None, bool | None, bool, int]:
        try:
            raw = user_agent.encode("latin-1")
        except UnicodeEncodeError:
            raw = user_agent.encode("utf-8")
        return self._database.classify(raw)

    def _blocked(self, user_agent: str, packed_rule_ids: bytes) -> bool:
        try:
            raw = user_agent.encode("latin-1")
        except UnicodeEncodeError:
            raw = user_agent.encode("utf-8")
        return self._database.blocked(raw, packed_rule_ids)


class WreathGeoIP:
    """Wreath's bundled, first-party compact country database.

    WGD2 stores exact, non-overlapping IP ranges in at most 20,000 bytes. The
    native database expands them once into operation-owned arrays and indexes
    each address family by its first byte before binary search. It deliberately
    returns `None` outside stored ranges instead of guessing, and creates only
    the final country code at the Python boundary.

    The bundled database is country-only. Pass a path to load another WGD2
    image; pass any `GeoIPProvider` to `ClientFactsProvider` for richer facts.
    """

    __slots__ = ("_database",)

    def __init__(self, database: str | Path | None = None) -> None:
        image = (
            _builtin_database("country.wgd")
            if database is None
            else Path(database).read_bytes()
        )
        self._database = _core.GeoDB(image)

    def lookup(self, address: str) -> GeoIPRecord | None:
        packed = ipaddress.ip_address(address).packed
        country = self._database.lookup(packed)
        return None if country is None else GeoIPRecord(country=country)
