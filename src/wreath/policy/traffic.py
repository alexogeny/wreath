"""Declarative traffic classes derived from Wreath client facts.

Classification runs after trusted proxy handling and before rate limiting. It
establishes a bounded, application-declared name that existing rate-limit and
Cedar policy can consume; it is not a second authorization engine. User-Agent,
Client Hints, and GeoIP remain evidence rather than identity. Only a verified
agent identity is suitable for a trust-sensitive class.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from struct import pack
from typing import Any

from .._flight_schema import FLAG_AI_SCRAPING_REFUSED, FLAG_POLICY_REFUSED
from .._native import _core
from ..client_facts import ClientFacts, ClientFactsProvider, UserAgentDatabase
from ..response import ProblemResponse

_STATE_SLOT = "_traffic_class"
_UNCLASSIFIED = "unclassified"

# Product tokens in the bundled WUA image that autonomously crawl or assemble
# corpora for AI systems. User-triggered fetchers (`chatgpt-user`, `claude-user`,
# `perplexity-user`) are deliberately absent: a person asking a service to open
# one URL is not the bulk scraping this default refuses.
AI_SCRAPERS = (
    "google-cloudvertexbot",
    "gptbot",
    "oai-searchbot",
    "claudebot",
    "claude-searchbot",
    "anthropic-ai",
    "perplexitybot",
    "amazonbot",
    "bytespider",
    "cohere-ai",
    "ai2bot",
    "diffbot",
    "youbot",
    "imagesiftbot",
    "omgilibot",
    "omgili",
    "webzio-extended",
    "ccbot",
)

_AI_REFUSAL_FLAGS = FLAG_POLICY_REFUSED | FLAG_AI_SCRAPING_REFUSED


def _record_ai_refusal(target: Any) -> None:
    context = getattr(target, "_context", target)
    record = getattr(context, "_flight_policy_refusal", None)
    if callable(record):
        record(_AI_REFUSAL_FLAGS)


def _strings(values: tuple[str, ...], *, field: str) -> frozenset[str]:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"traffic class {field} values must be non-empty strings")
    return frozenset(values)


def _membership[T](values: tuple[T, ...]) -> tuple[T, ...] | frozenset[T]:
    """Compile only lists long enough for hashing to repay its fixed cost."""
    return frozenset(values) if len(values) >= 4 else values


class AIScrapingPolicy:
    """Refuse autonomous AI crawlers unless the application opts in.

    Wreath installs this policy with no allowances by default. `allow=True`
    permits every known AI scraper; a tuple permits only the named product
    tokens and continues refusing the rest. User-triggered AI fetchers are not
    classified as scrapers and therefore remain allowed.

    The policy owns one native WUA database. Its frozen form is that database
    plus a packed, sorted rule-id table, so native ingress scans the User-Agent
    once and returns a refusal without constructing a `Request` or Python fact
    objects.
    """

    __slots__ = ("_blocked_products", "_blocked_table", "_database")

    def __init__(
        self,
        *,
        allow: bool | tuple[str, ...] = False,
        _database: UserAgentDatabase | None = None,
    ) -> None:
        if type(allow) is bool:
            allowed = frozenset(AI_SCRAPERS if allow else ())
        elif type(allow) is tuple:
            allowed = _strings(allow, field="AI scraper allow")
            unknown = sorted(allowed.difference(AI_SCRAPERS))
            if unknown:
                raise ValueError(
                    "unknown AI scraper allowance "
                    f"{unknown[0]!r}; use one of {', '.join(AI_SCRAPERS)}"
                )
        else:
            raise TypeError("AI scraper allow must be bool or a tuple of product tokens")
        database = UserAgentDatabase() if _database is None else _database
        blocked: list[int] = []
        for product in AI_SCRAPERS:
            if product not in allowed:
                rule_id = database._classify(product)[5]
                if rule_id == 0:
                    raise RuntimeError(
                        f"bundled User-Agent database has no AI scraper rule for {product!r}"
                    )
                blocked.append(rule_id)
        blocked.sort()
        self._database = database
        self._blocked_products = tuple(product for product in AI_SCRAPERS if product not in allowed)
        self._blocked_table = b"".join(pack("<H", rule_id) for rule_id in blocked)

    @property
    def blocked_products(self) -> tuple[str, ...]:
        """The stable product-token vocabulary this policy refuses."""
        return self._blocked_products

    def _ingress_sync(self, request: Any) -> ProblemResponse | None:
        if not self._blocked_table:
            return None
        if request.path == "/robots.txt" and request.method in {"GET", "HEAD"}:
            return None
        raw = request.header("user-agent", "") or ""
        try:
            blocked = self._database._blocked(raw, self._blocked_table)
        except ValueError:
            return None
        if not blocked:
            return None
        _record_ai_refusal(request)
        return ProblemResponse(
            status=403,
            title="Forbidden",
            detail="AI scraper traffic is disabled by default",
        )

    def _ingress_scope(self, scope: Any, method: str, path: str) -> ProblemResponse | None:
        """Run portable ASGI ingress without materializing a Request."""
        if not self._blocked_table or (path == "/robots.txt" and method in {"GET", "HEAD"}):
            return None
        headers = scope["headers"] if isinstance(scope, dict) else scope.headers
        if not self._database._database.blocked_headers(headers, self._blocked_table):
            return None
        _record_ai_refusal(scope)
        return ProblemResponse(
            status=403,
            title="Forbidden",
            detail="AI scraper traffic is disabled by default",
        )

    def _native(self) -> tuple[Any, bytes] | None:
        if not self._blocked_table:
            return None
        return self._database._database, self._blocked_table

    def counters(self) -> Any:
        """The native refusal total, discovered by every metrics bridge."""
        from ..metrics import Counters

        return Counters(
            "ai_scraping_policy",
            "default",
            {"refused": self._database._database.blocked_count()},
        )


@dataclass(frozen=True, slots=True)
class TrafficClass:
    """One conjunctive client-fact match and its ingress disposition.

    Omitted fields are unconstrained; supplied fields are ANDed. Multiple
    values inside one field are alternatives. The first declaration matching a
    request wins, making precedence visible in the tuple handed to
    `TrafficPolicy`.

    `countries`, `browsers`, `claimed_agent` and `mobile` are asserted
    or coarse lookup facts. They are useful for shaping traffic, rendering, and
    analytics, but must not be treated as authentication. `verified_agent`
    and `agent_identities` come from accepted Web Bot Auth signatures.
    """

    name: str
    countries: tuple[str, ...] = ()
    browsers: tuple[str, ...] = ()
    ip_versions: tuple[int, ...] = ()
    address_sources: tuple[str, ...] = ()
    agent_identities: tuple[str, ...] = ()
    claimed_agent: bool | None = None
    verified_agent: bool | None = None
    mobile: bool | None = None
    deny: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.isascii():
            raise ValueError("traffic class name must be non-empty ASCII")
        countries = _strings(self.countries, field="countries")
        invalid_country = next(
            (
                value
                for value in countries
                if len(value) != 2
                or not value.isascii()
                or not value.isalpha()
                or value != value.upper()
            ),
            None,
        )
        if invalid_country is not None:
            raise ValueError(
                f"traffic class country {invalid_country!r} must be an uppercase "
                "two-letter ISO code"
            )
        if any(isinstance(version, bool) or version not in (4, 6) for version in self.ip_versions):
            raise ValueError("traffic class ip_versions may contain only 4 and 6")
        invalid_source = next(
            (source for source in self.address_sources if source not in {"socket", "forwarded"}),
            None,
        )
        if invalid_source is not None:
            raise ValueError(
                f"traffic class address source {invalid_source!r} must be 'socket' or 'forwarded'"
            )
        _strings(self.browsers, field="browsers")
        _strings(self.agent_identities, field="agent_identities")
        if not any(
            (
                self.countries,
                self.browsers,
                self.ip_versions,
                self.address_sources,
                self.agent_identities,
                self.claimed_agent is not None,
                self.verified_agent is not None,
                self.mobile is not None,
            )
        ):
            raise ValueError(
                f"traffic class {self.name!r} has no match criteria; use the "
                "TrafficPolicy default for unmatched requests"
            )

    def matches(self, facts: ClientFacts) -> bool:
        """Whether every declared condition holds for `facts`."""
        return _traffic_matches(self, facts)


@dataclass(frozen=True, slots=True)
class _CompiledTrafficClass:
    """One startup-compiled matcher for a longer public declaration."""

    name: str
    countries: tuple[str, ...] | frozenset[str]
    browsers: tuple[str, ...] | frozenset[str]
    ip_versions: tuple[int, ...]
    address_sources: tuple[str, ...]
    agent_identities: tuple[str, ...] | frozenset[str]
    claimed_agent: bool | None
    verified_agent: bool | None
    mobile: bool | None
    deny: bool

    @classmethod
    def compile(cls, declaration: TrafficClass) -> _CompiledTrafficClass:
        return cls(
            declaration.name,
            _membership(declaration.countries),
            _membership(declaration.browsers),
            declaration.ip_versions,
            declaration.address_sources,
            _membership(declaration.agent_identities),
            declaration.claimed_agent,
            declaration.verified_agent,
            declaration.mobile,
            declaration.deny,
        )

    def matches(self, facts: ClientFacts) -> bool:
        return _traffic_matches(self, facts)


def _traffic_matches(rule: Any, facts: ClientFacts) -> bool:
    """The one predicate used by declarations and membership-compiled rules."""
    ip = facts.ip
    geo = None if ip is None else ip.geo
    country = None if geo is None or geo.country is None else geo.country.upper()
    ua = facts.user_agent
    agent = facts.agent
    return (
        (not rule.countries or country in rule.countries)
        and (not rule.browsers or ua.browser in rule.browsers)
        and (not rule.ip_versions or (ip is not None and ip.version in rule.ip_versions))
        and (not rule.address_sources or (ip is not None and ip.source in rule.address_sources))
        and (
            not rule.agent_identities
            or (agent.verified and agent.identity in rule.agent_identities)
        )
        and (rule.claimed_agent is None or agent.claimed is rule.claimed_agent)
        and (rule.verified_agent is None or agent.verified is rule.verified_agent)
        and (rule.mobile is None or ua.mobile is rule.mobile)
    )


def _compile_traffic_class(
    declaration: TrafficClass,
) -> TrafficClass | _CompiledTrafficClass:
    if (
        type(declaration) is not TrafficClass
        or max(
            len(declaration.countries),
            len(declaration.browsers),
            len(declaration.agent_identities),
        )
        < 4
    ):
        return declaration
    return _CompiledTrafficClass.compile(declaration)


class TrafficPolicy:
    """Classify each request once, optionally refusing selected classes.

    The selected name is written to request state before rate limiting and
    authorization. `traffic_class(request)` reads it for a custom limiter
    tier/key, and Wreath's default Cedar context publishes it as
    `context.client_class`. A denied class returns one uniform 403 response;
    the response does not reveal which fact matched.

    This component deliberately keeps no callback predicate. The complete
    matching vocabulary is validated at construction, and every class name is
    bounded by application configuration. A custom decision belongs in Cedar,
    which receives the selected class rather than a parallel policy language.
    """

    __slots__ = ("_classes", "_counts", "_default", "_lock", "_provider")

    def __init__(
        self,
        provider: ClientFactsProvider,
        classes: tuple[TrafficClass, ...],
        *,
        default: str = _UNCLASSIFIED,
    ) -> None:
        if not isinstance(provider, ClientFactsProvider):
            raise TypeError("traffic policy provider must be a ClientFactsProvider")
        if not classes:
            raise ValueError(
                "traffic policy needs at least one TrafficClass; omit the policy "
                "when every request uses the default"
            )
        if not default or not default.isascii():
            raise ValueError("traffic policy default class must be non-empty ASCII")
        names = [declaration.name for declaration in classes]
        duplicate = _core.first_duplicate(names)
        if duplicate is not None:
            raise ValueError(f"duplicate traffic class: {duplicate!r}")
        if default in names:
            raise ValueError(
                f"traffic policy default {default!r} duplicates a declared class; "
                "the default is only for unmatched requests"
            )
        self._provider = provider
        self._classes = tuple(_compile_traffic_class(declaration) for declaration in classes)
        self._default = default
        self._lock = threading.Lock()
        self._counts = {"classified": 0, "matched": 0, "unmatched": 0, "denied": 0}

    def _ingress_sync(self, request: Any) -> ProblemResponse | None:
        facts = self._provider.resolve(request)
        selected = next(
            (declaration for declaration in self._classes if declaration.matches(facts)),
            None,
        )
        name = self._default if selected is None else selected.name
        request.state.__setattr__(_STATE_SLOT, name)
        denied = selected is not None and selected.deny
        with self._lock:
            self._counts["classified"] += 1
            self._counts["matched" if selected is not None else "unmatched"] += 1
            self._counts["denied"] += int(denied)
        if denied:
            return ProblemResponse(
                status=403,
                title="Forbidden",
                detail="Client traffic policy refused this request",
            )
        return None

    async def _ingress(self, request: Any) -> ProblemResponse | None:
        """Reference executor wrapper for the fixed synchronous program."""
        return self._ingress_sync(request)

    def counters(self) -> Any:
        """Bounded aggregate readings for the existing metrics bridges."""
        from ..metrics import Counters

        with self._lock:
            values = dict(self._counts)
        return Counters("traffic_policy", "default", values)


def traffic_class(request: Any) -> str:
    """The class selected for this request, or `"unclassified"`.

    This is the tier callback to hand an existing `TieredRateLimitPolicy`.
    It never resolves facts itself: `TrafficPolicy` owns that work and runs
    before rate limiting.
    """
    state = getattr(request, "state", None)
    get = getattr(state, "get", None)
    if not callable(get):
        return _UNCLASSIFIED
    value = get(_STATE_SLOT)
    return value if isinstance(value, str) else _UNCLASSIFIED


__all__ = ["TrafficClass", "TrafficPolicy", "traffic_class"]
