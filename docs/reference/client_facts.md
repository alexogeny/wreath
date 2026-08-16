# `wreath.client_facts`

Trust-aware client IP and browser facts. The provider reads the socket peer or
the address accepted by `ProxyPolicy`; it never trusts forwarding headers by
itself. Client Hints and User-Agent values are suitable for presentation and
analytics, not authorization.

This module ships two actual Wreath databases, not only adapters:

- `WreathGeoIP()` loads the bundled 19,996-byte WGD2 country database. It holds
  2,281 exact IPv4 ranges across 251 countries and 318 exact IPv6 range runs.
  A first-byte directory narrows each lookup before native binary search;
  uncovered addresses remain unknown rather than inheriting an approximate
  supernet.
- `UserAgentDatabase()` loads the bundled 4,969-byte WUA1 product database. Its
  197 entries cover major and regional browsers, in-app clients, HTTP and cloud
  SDKs, health checks, monitors, feeds, search crawlers, AI training/search/user
  agents, user-owned crawler frameworks, and headless browsers. A native hash
  index and one scan classify them; the request path creates only the final
  result tuple, not one Python object per rule.

Both images are copied into operation-owned native memory when their provider
is constructed. Neither has a process-global mutable cache. WGD2 refuses an
image over 20,000 bytes and WUA1 refuses one over 5,000 bytes; the generator
enforces the same ceilings.

## AI scraping is denied unless the application opts in

A bare `Wreath()` refuses known autonomous AI crawlers before routing. The
bundled WUA database is scanned in native policy ingress, and a match returns a
uniform 403 without constructing a Python `Request` or client-fact objects.
Ordinary bots are not covered by this default, and neither are user-triggered
fetchers such as `ChatGPT-User`, `Claude-User`, and `Perplexity-User`: the
default is about autonomous scraping, not every request involving an AI tool.

Opt into all known AI scrapers explicitly:

```python
app = Wreath(ai_scraping="allow")
```

Or retain the default refusal while admitting named product tokens:

```python
from wreath.policy import AIScrapingPolicy, HttpPolicy

app = Wreath(http_policy=HttpPolicy(
    ai_scraping=AIScrapingPolicy(allow=("oai-searchbot",)),
))
```

The same declaration may be applied after constructing a bare application;
an explicit policy replaces Wreath's injected default rather than colliding
with it:

```python
app = Wreath()
app.configure_http_policy(HttpPolicy(
    ai_scraping=AIScrapingPolicy(allow=("oai-searchbot",)),
))
```

`AI_SCRAPERS` is the complete accepted vocabulary. An unknown name is refused
at construction rather than silently permitting nothing. To admit scraper
traffic with a bounded allowance, opt in and compose `TrafficPolicy` with the
existing `TieredRateLimitPolicy`; classification does not grow a second token
bucket implementation.

`robots_txt(app)` reflects the same policy. It emits one specific group for
the autonomous products the application blocks, with `Disallow: /`, before the
route-derived `User-agent: *` group. `GET` and `HEAD /robots.txt` are therefore
the only paths the enforced AI policy exempts: a well-behaved crawler can read
the refusal before attempting content, while a `POST` or a lookalike path is
still refused.

Refusals increment the application-owned
`ai_scraping_policy.refused` aggregate, which `app.metrics(...)` exposes to
Prometheus and which the StatsD/DogStatsD bridge discovers through the same
counter registry. With the native server, each refusal is also a Flight
completion carrying `policy_disposition=ai_scraping`; OTLP projects that as
`wreath.policy.refused=true` and `wreath.policy.disposition=ai_scraping`.
Expected 403 decisions are deliberately not reported to Sentry as exceptions.

## What the default can and cannot prove

The default is an enforced filter for **declared, known autonomous crawlers**.
It is not a claim that Wreath can recognize every scraper. `User-Agent` is
caller-controlled: an undeclared crawler can send an ordinary Chrome string,
and a distributed scraper can rotate addresses and networks. Cloudflare has
documented that exact combination in production: a generic Chrome identity,
unpublished IPs, and ASN rotation after declared Perplexity traffic was blocked.
The IETF is equally explicit that `robots.txt` rules are requests, not access
authorization.

- <https://blog.cloudflare.com/perplexity-is-using-stealth-undeclared-crawlers-to-evade-website-no-crawl-directives/>
- <https://datatracker.ietf.org/doc/html/rfc9309#section-3>

There is no origin-framework test that proves an unsigned, browser-shaped
request came from a human. Build the boundary from what each signal can prove:

1. Put non-public or high-value content behind Wreath authentication and Cedar
   authorization. A URL that must not be copied must not be public; `robots.txt`,
   `noindex`, a User-Agent rule, and an IP range are not substitutes.
2. Keep the default AI refusal and publish `robots_txt(app)` for transparent
   operators. Use distinct product allowances only when search/retrieval value
   is worth the crawl.
3. Configure `Signatures` and make `verified_agent` plus `agent_identities` the
   allow-list signal for an agent that must receive privileged treatment. Web
   Bot Auth uses HTTP Message Signatures to prove a positive operator identity.
   A missing or invalid signature remains unknown; it never proves “human”.
4. Bound anonymous work with a global IP-keyed `RateLimitPolicy`, then apply
   identity- or traffic-class tiers after authentication. Rate limits do not
   identify a scraper, but they cap what a spoofed identity can cost or collect.
5. Put a bot-management edge in front of valuable public corpora when the
   threat includes browser emulation or distributed residential/cloud traffic.
   TLS/browser fingerprints, cross-site reputation, JavaScript challenges, and
   network-wide behavior do not survive as trustworthy facts at an ASGI origin.
   Accept an edge decision only through a `ProxyPolicy` trust boundary.

For legacy search crawlers that do not sign, verify their published CIDR feed or
perform forward-confirmed reverse DNS rather than trusting their User-Agent.
Google documents both methods and warns that its User-Agent is commonly
spoofed. Anthropic, by contrast, currently says it uses service-provider public
IPs and does not publish stable ranges; an IP-only design therefore cannot be a
universal crawler identity layer.

- <https://developers.google.com/crawling/docs/crawlers-fetchers/verify-google-requests>
- <https://support.anthropic.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler>
- <https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/>

```python
app = Wreath(signatures=signatures)
facts = app.client_facts("public")

@app.get("/dashboard", dependencies=(Depends(facts),))
async def dashboard(request: Request): ...
```

`app.client_facts()` defaults to Wreath's native country and UA databases,
registers the provider for application metrics, exposes it as
`app.state.client_facts_<name>`, and connects the app's Web Bot Auth signature
layer. The provider is itself a dependency. Its answer is cached by provider
identity on `request.state`, so a dependency, handler, and error reporter all
receive one immutable value and increment counters once. Constructing
`ClientFactsProvider` directly remains available for a provider not owned by a
`Wreath` application.

## Turn facts into one bounded traffic class

`TrafficPolicy` is the action seam. It evaluates an ordered tuple of declarative
`TrafficClass` matches once at ingress and publishes the selected name through
`traffic_class(request)` and Cedar's `context.client_class`:

```python
from wreath.policy import HttpPolicy, TrafficClass, TrafficPolicy

facts = app.client_facts("public")
app.configure_http_policy(HttpPolicy(traffic=TrafficPolicy(facts, (
    TrafficClass("verified-agent", verified_agent=True),
    TrafficClass("unverified-bot", claimed_agent=True, verified_agent=False),
    TrafficClass("blocked-region", countries=("AQ",), deny=True),
))))
```

First match wins; an unmatched request is `unclassified`. Existing
`TieredRateLimitPolicy` can use `tier=traffic_class`, while Cedar can permit or
forbid against the same selected name. A denied class stops before routing with
one uniform 403. Classification totals are discovered by the same metrics walk
as the provider's counters.

Country, browser, mobile status, and a claimed bot are not authentication and
must not grant access. `verified_agent=True` and `agent_identities=` are backed
by the configured Web Bot Auth verifier; those are the fields intended for a
trust-sensitive class. The policy deliberately accepts no arbitrary predicate:
an authorization decision belongs in Cedar rather than in a second callback
policy language.

## Observability without identifiers or unbounded labels

Every provider owns fixed aggregate counters for resolutions, UA classification,
known and forwarded addresses, IP family, successful Geo lookup, UA-claimed bot
traffic, verified-agent traffic, claimed-and-verified traffic, and mobile
traffic. It also exposes one `country_xx` total per observed valid
ISO alpha-2 country. That vocabulary has a hard 676-value ceiling. Pass the
provider through a bridge's `counter_sources=` argument:

```python
provider = ClientFactsProvider(geoip=WreathGeoIP(), name="public")
prometheus = telemetry.activate_prometheus(
    projector,
    counter_sources=(provider,),
)
datadog = telemetry.activate_statsd(
    projector,
    dogstatsd=True,
    counter_sources=(provider,),
)
```

An app-owned provider is discovered automatically by `app.metrics(...)` and by
any bridge constructed with `app=app`; `counter_sources` is for a standalone
provider or another explicit owner.

These metrics deliberately do not label by browser, platform, IP, User-Agent,
or country. Country is a bounded metric-name suffix instead; Client Hints are
untrusted strings, so using their values as labels would let a caller create an
unbounded series set. `client_fact_attributes(facts)`
instead produces per-event OpenTelemetry names such as
`geo.country.iso_code`, `user_agent.name`, and
`user_agent.synthetic.type`; it omits the IP and original User-Agent by
default. `telemetry.annotate_otel(span, facts)` applies the same projection to
an application-owned recording span.

When the native Flight Recorder is active, resolving a provider also attaches a
compact client-facts cell to that request. It contains only fixed flags, a
stable WUA rule id, and an ISO country code -- never the raw address,
User-Agent, Client Hint, or `Signature-Agent`. The off-path projector adds those
bounded facts to both OTLP/JSON and Wreath's direct OTLP/protobuf server span.

`ClientFacts.agent` keeps UA claim and cryptographic proof separate:
`claimed` comes from WUA classification, while `verified` and `identity` come
only from a successful Web Bot Auth signature. An unverified caller cannot
promote its `Signature-Agent` string into the identity field.

Sentry can receive that projection on only the error being reported:

```python
app.add_error_reporter(SentryErrorReporter(client_facts=provider))
```

Country, IP family, address source, mobile, and bot status become bounded Sentry
tags. Browser, version, and platform remain non-indexed event context. The raw
IP, city, coordinates, ASN organization, and original User-Agent are absent.

The bundled GeoIP data is a deliberately small country-level database, not an
exhaustive source of city, coordinate, or ASN facts. A deployment can implement
the `GeoIPProvider` protocol and pass it to `ClientFactsProvider`; the protocol
returns the same `GeoIPRecord`, so richer databases remain pluggable without a
vendor-specific framework surface. `UserAgentDatabase.from_path` accepts a
regenerated WUA1 image or a JSON rule file for a deployment-specific vocabulary.

## Format and selection

WGD2 stores arbitrary ranges rather than splitting them into CIDR prefixes.
Sorted starts become varint gaps from the preceding range, inclusive spans are
varints, and the country is a one-byte index into a shared table. IPv6 stores
exact runs of `/64` blocks, avoiding a platform-specific 128-bit runtime type.
The selection first preserves the largest IPv4 range for every country, reserves
4,500 encoded bytes for the largest IPv6 ranges, then fills the remaining bound
with the largest IPv4 ranges. It covers 56.25% of the IPv4 address space without
widening a single range.

WUA1 deduplicates browser and platform result strings, then encodes each product
token with result indexes, flags, and priority. Construction creates the native
hash table once; lookup performs one bounded token scan rather than a regex pass
per known product. Rebuild both images with
`tools/generate_client_facts_data.py`.

::: wreath.client_facts
