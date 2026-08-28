---
description: SAML and OIDC login, with one identity provider per organisation — and an authorization server for a deployment that issues its own tokens.
keywords: sso, saml, oidc, okta, entra, azure ad, google workspace, jit provisioning, acs, oauth, authorization server, enterprise login
---

# Single sign-on

Wreath has always been able to *verify* the hard parts.
[`wreath.saml`](signed-xml.md) checks a signed assertion — exclusive
canonicalization, signature wrapping made unexpressible, a replay ledger, every
refusal named — and [`wreath.auth`](auth.md) verifies an OIDC id token. What
neither had was a **login**: no `AuthnRequest`, no assertion consumer, no
metadata, no callback. `wreath.sso` is the flows.

```python
from wreath.sso import (
    IdentityProviderConfig, IdentityProviderDirectory, SamlServiceProvider,
)

directory = IdentityProviderDirectory([
    IdentityProviderConfig(
        organization="acme",
        entity_id="https://acme.okta.com/exk1",
        sso_url="https://acme.okta.com/app/sso/saml",
        certificates=(acme_signing_certificate,),
    ),
])
sp = SamlServiceProvider(
    entity_id="https://app.example/saml",
    acs_url="https://app.example/sso/acs",
    directory=directory,
)
```

## The part that is not glue

A single-tenant application configures one identity provider and is finished. A
B2B application has **one per customer** — Okta here, Entra there, Google
Workspace for the small ones — and that changes the threat model rather than the
amount of configuration.

Every tenant's identity provider is a trusted signer *of that tenant, and of
nothing else*. Verify an assertion against the union of every configured
certificate and the smallest customer's Okta can mint an identity in the largest
customer's account — and **no signature check fails while it happens**, because
the assertion really is signed by a key this application really does trust.

So the signer set is scoped to the organisation, and the organisation comes from
**the login that began**:

```python
browser_binding = request.state.session.setdefault("sso_binding", secrets.token_urlsafe(32))
begun = sp.begin_login(organization="acme", session_id=browser_binding)
# ... browser round trip ...
verified = await sp.consume(
    raw,
    in_response_to=begun.request_id,
    relay_state=relay_state,
    session_id=browser_binding,
    ledger=ledger,
)
```

Reading the organisation out of the *assertion* would let the assertion choose
its own trust anchor, which is the same defect one indirection further along.

`tests/sso/test_saml_flow.py` proves this with two real signing identities: an
assertion that is correctly signed, in date, for the right audience, answering a
request this application issued — and signed by the wrong customer's provider.
It is refused, and widening the signer set to the union makes the same test
accept it.

## The request id is what makes a response solicited

`begin_login` mints one and stores it. Without it, any assertion the identity
provider ever signed is a login whenever it arrives — which is a login as
anybody, from a captured POST body. It is single-use, so a replayed body finds
nothing pending, and the SAML replay ledger is the second layer behind it.

## Provisioning is declared, never inferred

```python
mapping = AttributeMapping(email="mail", display_name="displayName")
```

An attribute the mapping does not name is **refused**, not dropped. A heuristic
that reads `email` and misses `mail` is confident and wrong, and the failure is
one duplicate account per user per identity provider.

`JitProvisioning` creates the account and the membership together — an account
with no membership sees nothing and reads as a bug in login — and **adopts an
existing account rather than duplicating it**, the same choice
[`scim_router`](scim.md)'s `POST /Users` makes: somebody who signed up with a
password before their company bought SSO keeps their data.

A role outside the declared vocabulary is refused **at configuration time**,
because an identity-provider attribute is whatever a customer's directory admin
typed and it must not be able to name `admin`. And where the organisation
requires a second factor, the login yields a **pending** session — which
`SessionIdentityBackend` already refuses to turn into an identity, so this
composes with [second factors](second-factors.md) rather than bypassing them.

## OIDC

Three single-use values, each defending a different thing:

- **PKCE (S256 only).** `plain` sends the verifier *as* the challenge and is
  refused at construction.
- **`state`, bound to the session that began the flow.** Without the binding, an
  attacker's authorization code redeemed in a victim's browser signs the victim
  into the *attacker's* account, where everything they then do is visible.
- **`nonce`**, which binds the id token to this authorization request.

**Nothing is fetched on the request path.** Discovery and JWKS refresh at
lifespan startup and on an explicit `refresh()`, exactly as
[`wreath.signatures`](verified-agents.md) refreshes its directories: a fetch
driven by a request lets an unauthenticated caller aim an outbound request at a
host they chose, and puts the identity provider's outage in front of every login
rather than in front of the refresh. An unknown `kid` is simply unverified.

## Issuing your own tokens

[`wreath.oauth`](../reference/oauth.md) is the other direction, for a deployment
that *is* an identity provider to its own machine clients rather than having
one:

```python
server = AuthorizationServer(issuer="https://app.example", secret=SETTINGS.oauth_secret)
token = server.redeem(
    code,
    verifier=verifier,
    client_id=client_id,
    client_secret=SETTINGS.oauth_client_secret,
    redirect_uri=redirect_uri,
)
```

**Every parameter is required, and each is a check RFC 6749 §4.1.3 asks of the
token endpoint.** The code is bound to the client it was issued to, the URI it
was issued for, and the PKCE challenge it was issued against, and it expires
after `code_ttl` (60 seconds by default) — an authorization code travels through
a browser redirect, so it reaches referrer headers, proxy logs and history, and
one that never goes stale is a password in a log file. `issue_code` likewise
takes `challenge` and `redirect_uri` with no defaults: an optional security
parameter is an optional security control.

The first obligation is that what it mints is what wreath already verifies —
an issuer whose output its own verifier rejects is two features rather than one
— so the tests drive a minted token through the real `JwtVerifier`.

Two signers, and the choice is about who verifies.

**`Es256Signer` is what most deployments want.** It signs with ECDSA on P-256,
`jwks()` publishes a real key set anybody can verify against, and it adds **no
dependency** — `wreath._webpush` already signs ES256 VAPID tokens over the
standard library and `JwtVerifier` already verifies ES256, so both halves of the
loop were in the tree already.

```python
server = AuthorizationServer(issuer="...", signer=Es256Signer.generate())
```

**HS256 is the default**, and that is about ceremony rather than strength: a
shared secret needs no key to generate, store, rotate or publish, which is right
when the issuer and the resource server are the same deployment. Its cost is
that there is nothing safe to publish, so `jwks()` is honestly empty — an `oct`
entry carrying `k` would hand every reader the ability to mint tokens.

The moment anything outside the deployment verifies your tokens, switch. The
price, measured over three warm runs rather than assumed:

| | per token |
| --- | --- |
| HS256 | 11.5–13.2 µs |
| ES256 | 2.91–3.01 ms |

About 230×. **The number that matters is not throughput, it is the tail.** ES256
signing is CPU-bound pure Python, so it holds the event loop while it runs, and
the request unlucky enough to be behind a signature waits the whole 3 ms:

| issuance rate | loop lag p50 | loop lag p99 |
| --- | --- | --- |
| ES256 at 10/s | 0.01 ms | 0.89 ms |
| ES256 at 50/s | 0.01 ms | 3.00 ms |
| ES256 at 200/s | 0.01 ms | 3.15 ms |
| HS256 at 200/s | 0.01 ms | 0.01 ms |

The median is untouched at every rate — most requests never collide with a
signature. Roughly 330 signatures a second saturates one core.

So ES256 is safe wherever tokens are minted at *human* rates: a login endpoint at
ten a second costs under a millisecond of tail. It is the wrong choice on a
machine-to-machine path minting short-lived tokens continuously — there, issue
long-lived tokens and refresh them rarely, which is what the shape is for anyway.

Rather than bake in a threshold that would be one guess about every deployment's
latency budget, the cost is counted: `AuthorizationServer.counters()` reports
`signing_seconds`, which [`wreath.metrics`](../reference/metrics.md) collects by
asking. Divided by wall time, that is the fraction of a core signing is spending
— which is the thing to alert on.

*(Key leakage is not a concern here: the scalar multiplication is
`_curves.p256_scalarmult_secret`, a constant-time Montgomery ladder, because
every scalar `_webpush` multiplies is secret.)*

Three replay defences, separate because they fail differently:

- **An authorization code is single use, and a second redemption revokes the
  token the first issued.** Refusing the second alone leaves the attacker's
  token live if they got there first, so when two parties demonstrably hold one
  code, neither keeps anything.
- **A refresh token rotates, and reuse revokes the whole chain.** Rotation
  without reuse detection is rotation that tells you nothing.
- **A redirect URI is matched exactly.** `https://app.example.evil/cb` has the
  registered URI as a prefix, and an open redirect on the authorization endpoint
  is an authorization-code exfiltration.

A client-credentials token carries no `sub` — a machine token naming a person is
a machine that can act as one, and every audit trail downstream then attributes
its writes to them — and a public client cannot use that grant at all. Tokens
carry their tenant, so they compose with [multi-tenancy](tenancy.md) rather than
around it.
