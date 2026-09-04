"""Single sign-on: the flows, over the verification wreath already owned.

`wreath.saml.verify_response` does the hard half — exclusive canonicalization,
signature wrapping made unexpressible by re-reading the parsed byte range, a
replay ledger, every refusal named. It was also the module's *only* public
symbol, so there was no `AuthnRequest`, no assertion-consumer endpoint, no
service-provider metadata, and no login. `wreath.auth` verifies an OIDC id token
and drives no relying-party flow. This module is the flows.

Nothing here re-implements verification. `SamlServiceProvider.consume` builds
the `IdentityProvider`, `ServiceProvider` and `in_response_to` that
`verify_response` already takes, and hands the answer to provisioning.

## The reason this is more than glue: one identity provider per organisation

A single-tenant application configures one IdP and is done. A B2B application
has one per customer -- Okta here, Entra there, Google Workspace for the small
ones -- and that changes the threat model rather than the amount of
configuration.

**Every tenant's IdP is a trusted signer of that tenant, and of nothing else.**
Verify an assertion against the union of every configured certificate and the
smallest customer's Okta can mint an identity in the largest customer's account,
with every signature check passing while it happens. There is no signature to
notice: the assertion really is signed by a key this application really does
trust.

So the signer set is scoped to the organisation, and the organisation comes from
**the login that began**, never from the assertion. Reading it out of the
assertion would let the assertion choose its own trust anchor, which is the same
defect one indirection further along. `PendingLogin` is what carries it: the
request id is minted here, stored with its organisation, and spent by the ACS.

## Two rules inherited from elsewhere in the tree

**Nothing is fetched on the request path.** OIDC discovery and JWKS refresh at
lifespan startup and on an explicit `refresh()`, exactly as `wreath.signatures`
refreshes its directories -- a key fetch driven by a request lets an
unauthenticated caller aim an outbound request at a host they chose, and puts an
identity provider's outage in the path of every login rather than of the
refresh. An unknown `kid` is simply unverified.

**Every refusal is distinct.** A test that asserts only "it was refused" passes
on whichever branch fired, including the one nobody was testing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

from ._capability_map import CapabilityMap

__all__ = [
    "AttributeMapping",
    "IdentityProviderConfig",
    "IdentityProviderDirectory",
    "JitProvisioning",
    "OidcRelyingParty",
    "PendingLogin",
    "ProvisionedLogin",
    "SamlServiceProvider",
    "SsoRefusal",
    "UnknownIdentityProvider",
]

_MAX_JWKS_KEYS = 128
_MAX_KID_LENGTH = 256
_MAX_SIGNING_CERTIFICATES = 16


class SsoRefusal(Exception):
    """A login this application will not complete, and why.

    Carries a `reason` code as well as prose so a caller can branch without
    matching on a message, the way `wreath.saml.SamlRefusal` already does.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnknownIdentityProvider(SsoRefusal):
    """No identity provider is configured for that organisation."""

    def __init__(self, organization: str) -> None:
        super().__init__(
            "unknown-idp",
            f"no identity provider is configured for organisation {organization!r}; "
            "an SSO login is per organisation and there is no default",
        )


@dataclass(frozen=True, slots=True)
class IdentityProviderConfig:
    """One organisation's identity provider.

    `certificates` are that organisation's signing certificates and **only**
    that organisation's. More than one during a rotation, which is why this is a
    tuple rather than a single value: the old and new can both be live while the
    directory switches over.
    """

    organization: str
    entity_id: str
    #: Where an `AuthnRequest` is sent (HTTP-Redirect or HTTP-POST binding).
    sso_url: str
    certificates: tuple[str, ...]
    #: Attribute names, mapped to the fields this application stores.
    mapping: AttributeMapping | None = None
    #: Roles a login through this provider grants. Must be in the organisation's
    #: own vocabulary; an IdP attribute cannot name one.
    roles: tuple[str, ...] = ()
    require_second_factor: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.certificates, (str, bytes)):
            raise SsoRefusal(
                "invalid-signer-configuration",
                "identity-provider certificates must be an iterable of complete certificate texts",
            )
        if isinstance(self.roles, str):
            raise SsoRefusal(
                "invalid-role-configuration",
                "identity-provider roles must be an iterable of complete role names, not text",
            )
        object.__setattr__(self, "certificates", tuple(self.certificates))
        object.__setattr__(self, "roles", tuple(self.roles))
        if any(
            not isinstance(certificate, str) or not certificate.strip()
            for certificate in self.certificates
        ):
            raise SsoRefusal(
                "invalid-signer-configuration",
                "identity-provider certificates must contain only non-empty certificate text",
            )
        if any(not isinstance(role, str) or not role for role in self.roles):
            raise SsoRefusal(
                "invalid-role-configuration",
                "identity-provider roles must contain only non-empty text role names",
            )
        if not isinstance(self.require_second_factor, bool):
            raise SsoRefusal(
                "invalid-second-factor",
                "identity-provider require_second_factor must be a boolean",
            )
        if not self.certificates:
            raise SsoRefusal(
                "no-signer",
                f"identity provider for {self.organization!r} has no signing certificate, "
                "so no assertion from it could ever be verified",
            )
        if len(self.certificates) > _MAX_SIGNING_CERTIFICATES:
            raise SsoRefusal(
                "too-many-signers",
                f"identity provider for {self.organization!r} accepts at most "
                f"{_MAX_SIGNING_CERTIFICATES} signing certificates",
            )
        try:
            parsed_sso_url = urlsplit(self.sso_url)
            _ = parsed_sso_url.port
        except ValueError as error:
            raise SsoRefusal(
                "insecure-sso-url",
                f"identity provider SSO URL {self.sso_url!r} must be an absolute HTTPS "
                "URL without credentials, controls, or a fragment",
            ) from error
        if (
            any(
                ord(character) < 0x21 or 0x7F <= ord(character) <= 0x9F
                for character in self.sso_url
            )
            or parsed_sso_url.scheme != "https"
            or parsed_sso_url.hostname is None
            or parsed_sso_url.username is not None
            or parsed_sso_url.port == 0
            or parsed_sso_url.fragment
            or "\\" in self.sso_url
        ):
            raise SsoRefusal(
                "insecure-sso-url",
                f"identity provider SSO URL {self.sso_url!r} must be an absolute HTTPS "
                "URL without credentials, controls, or a fragment",
            )


class IdentityProviderDirectory:
    """Which identity provider signs for which organisation.

    A miss is a refusal, never a fallback to "any configured provider" -- that
    fallback *is* the cross-tenant forgery this module exists to prevent.
    """

    __slots__ = ("_by_organization",)

    def __init__(self, providers: Iterable[IdentityProviderConfig] = ()) -> None:
        self._by_organization: dict[str, IdentityProviderConfig] = {}
        for provider in providers:
            if provider.organization in self._by_organization:
                raise SsoRefusal(
                    "duplicate-idp",
                    f"duplicate identity provider for organisation {provider.organization!r}; "
                    "each organisation must select exactly one trust anchor",
                )
            self._by_organization[provider.organization] = provider

    def for_organization(self, organization: str) -> IdentityProviderConfig:
        provider = self._by_organization.get(organization)
        if provider is None:
            raise UnknownIdentityProvider(organization)
        return provider

    def add(self, provider: IdentityProviderConfig) -> None:
        if provider.organization in self._by_organization:
            raise SsoRefusal(
                "duplicate-idp",
                f"duplicate identity provider for organisation {provider.organization!r}; "
                "each organisation must select exactly one trust anchor",
            )
        self._by_organization[provider.organization] = provider

    def organizations(self) -> tuple[str, ...]:
        return tuple(self._by_organization)


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """An `AuthnRequest` this application issued and has not yet seen answered.

    The `request_id` is what makes a response *solicited*. Without one, any
    assertion the identity provider ever signed is accepted whenever it arrives,
    which is a login as anybody from a captured POST body.

    It also carries the organisation, and that is the load-bearing part: the ACS
    reads the trust anchor from here rather than from the assertion.
    """

    request_id: str
    relay_state: str = field(repr=False)
    organization: str
    issued_at: float
    session_id: str = field(default="", repr=False)
    #: Where to send the browser, with the request already encoded.
    redirect_url: str = ""


class PendingLoginStore:
    """Where a request id is spent, exactly once.

    In-process and bounded by its own expiry sweep. A fleet wants the same shape
    over `wreath.store`, whose `claim` makes the returned row *be* the
    consumption -- the property `_secondfactor`'s challenge store already has.
    """

    __slots__ = ("_by_id", "_max_entries", "_next_sweep", "_ttl")

    def __init__(self, *, ttl: float = 600.0, max_entries: int = 10_000) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not isfinite(ttl)
            or ttl <= 0
        ):
            raise ValueError("pending-login ttl must be a positive finite number")
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("pending-login max_entries must be a positive integer of at least one")
        self._by_id = CapabilityMap(
            max_entries=max_entries,
            ttl=ttl,
            clock=time.time,
            overflow="refuse",
        )
        self._ttl = ttl
        self._max_entries = max_entries
        self._next_sweep = float("inf")

    def put(self, pending: PendingLogin) -> None:
        if (
            isinstance(pending.issued_at, bool)
            or not isinstance(pending.issued_at, (int, float))
            or not isfinite(pending.issued_at)
        ):
            raise SsoRefusal("invalid-time", "the pending-login issue time must be finite")
        self._sweep(pending.issued_at)
        if len(self._by_id) >= self._max_entries:
            raise SsoRefusal(
                "pending-capacity",
                f"the pending-login store is at its ceiling of {self._max_entries}",
            )
        if not self._by_id.put(pending.request_id, pending, now=pending.issued_at):
            raise SsoRefusal(
                "pending-capacity",
                f"the pending-login store is at its ceiling of {self._max_entries}",
            )
        self._next_sweep = self._by_id.next_deadline

    def spend(
        self,
        request_id: str,
        *,
        relay_state: str,
        session_id: str,
        now: float | None = None,
    ) -> PendingLogin:
        """Take the pending login, or refuse. Never returns the same one twice."""
        moment = time.time() if now is None else now
        pending = self._by_id.held(request_id)
        if pending is None:
            raise SsoRefusal(
                "unsolicited",
                f"no login is pending for InResponseTo={request_id!r}: it was never "
                "issued, has already been spent, or has expired. An assertion that "
                "answers no request is unsolicited and is not a login here.",
            )
        if (
            isinstance(moment, bool)
            or not isinstance(moment, (int, float))
            or not isfinite(moment)
            or moment < pending.issued_at
        ):
            raise SsoRefusal(
                "invalid-time",
                "the clock is invalid or earlier than when this SAML login was issued",
            )
        if moment - pending.issued_at >= self._ttl:
            self._by_id.discard(request_id)
            raise SsoRefusal(
                "expired-request",
                f"the login for InResponseTo={request_id!r} was issued "
                f"{moment - pending.issued_at:.0f}s ago and the window is {self._ttl:.0f}s",
            )
        self._by_id.discard(request_id)
        if (
            not isinstance(relay_state, str)
            or not relay_state
            or not isinstance(session_id, str)
            or not session_id
        ):
            raise SsoRefusal(
                "state-session-mismatch",
                "SAML RelayState and its browser session binding are required",
            )
        relay_matches = hmac.compare_digest(pending.relay_state, relay_state)
        session_matches = hmac.compare_digest(pending.session_id, session_id)
        if not (relay_matches and session_matches):
            raise SsoRefusal(
                "state-session-mismatch",
                "this SAML response belongs to a different browser session",
            )
        return pending

    def organization_for(self, request_id: str) -> str:
        """Read the organisation without spending the request. For diagnostics."""
        pending = self._by_id.held(request_id)
        if pending is None:
            raise SsoRefusal("unsolicited", f"no login is pending for {request_id!r}")
        return pending.organization

    def _sweep(self, now: float) -> None:
        """Drop what has expired. Bounded work, amortised over insertions.

        A store that only ever grows is an in-process memory leak keyed by
        anybody who can start a login, which is everybody.
        """
        if now < self._next_sweep:
            return
        self._by_id.sweep(now=now)
        self._next_sweep = self._by_id.next_deadline


def _request_id() -> str:
    """A SAML `ID`, which is an XML `NCName` and so cannot start with a digit."""
    return f"_{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class SamlServiceProvider:
    """This application, as an identity provider has to address it.

    `entity_id` is what an `AudienceRestriction` must name, `acs_url` is what a
    bearer confirmation must equal, and `directory` says whose signature counts
    for which organisation.
    """

    entity_id: str
    acs_url: str
    directory: IdentityProviderDirectory
    #: The certificates *this* service provider signs its own requests with, if
    #: it signs them. Published in the metadata either way.
    certificates: tuple[str, ...] = ()
    pending: PendingLoginStore = field(default_factory=PendingLoginStore)

    def __post_init__(self) -> None:
        if isinstance(self.certificates, (str, bytes)):
            raise SsoRefusal(
                "invalid-service-provider-certificate",
                "SAML service-provider certificates must be an iterable of complete texts",
            )
        object.__setattr__(self, "certificates", tuple(self.certificates))
        if any(
            not isinstance(certificate, str) or not certificate.strip()
            for certificate in self.certificates
        ):
            raise SsoRefusal(
                "invalid-service-provider-certificate",
                "SAML service-provider certificates must contain only non-empty text",
            )
        if not isinstance(self.entity_id, str) or not self.entity_id:
            raise SsoRefusal("invalid-service-provider", "SAML entity_id must be non-empty text")
        try:
            parsed_acs = urlsplit(self.acs_url)
            _ = parsed_acs.port
        except (TypeError, ValueError) as error:
            raise SsoRefusal(
                "insecure-acs-url",
                "SAML assertion consumer URL must be an absolute HTTPS URL without "
                "credentials, controls, or a fragment",
            ) from error
        if (
            any(
                ord(character) < 0x21 or 0x7F <= ord(character) <= 0x9F
                for character in self.acs_url
            )
            or parsed_acs.scheme != "https"
            or parsed_acs.hostname is None
            or parsed_acs.username is not None
            or parsed_acs.port == 0
            or parsed_acs.fragment
            or "\\" in self.acs_url
        ):
            raise SsoRefusal(
                "insecure-acs-url",
                "SAML assertion consumer URL must be an absolute HTTPS URL without "
                "credentials, controls, or a fragment",
            )

    def metadata_xml(self) -> str:
        """The document an administrator pastes into their identity provider.

        Generated rather than written by hand: an `entityID` or an ACS URL that
        disagrees with what this application actually verifies is a login that
        fails with a signature error, which is the least informative way to
        discover a typo.
        """
        certificates = "".join(
            '<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>'
            f"<ds:X509Certificate>{_escape(_pem_body(certificate))}</ds:X509Certificate>"
            "</ds:X509Data></ds:KeyInfo></md:KeyDescriptor>"
            for certificate in self.certificates
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            'xmlns:ds="http://www.w3.org/2000/09/xmldsig#" '
            f"entityID={quoteattr(self.entity_id)}>"
            "<md:SPSSODescriptor protocolSupportEnumeration="
            '"urn:oasis:names:tc:SAML:2.0:protocol" '
            'AuthnRequestsSigned="false" WantAssertionsSigned="true">'
            f"{certificates}"
            "<md:AssertionConsumerService "
            'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location={quoteattr(self.acs_url)} index="0" isDefault="true"/>'
            "</md:SPSSODescriptor></md:EntityDescriptor>"
        )

    def begin_login(
        self,
        *,
        organization: str,
        session_id: str,
        now: float | None = None,
    ) -> PendingLogin:
        """Mint an `AuthnRequest` for one organisation and remember it.

        Resolving the provider *here* is what makes an unconfigured organisation
        a refusal at the start of a login rather than a signature failure at the
        end of one.
        """
        if not isinstance(session_id, str) or not session_id:
            raise SsoRefusal(
                "session-binding-required",
                "SAML login requires a non-empty browser session binding",
            )
        provider = self.directory.for_organization(organization)
        moment = time.time() if now is None else now
        pending = PendingLogin(
            request_id=_request_id(),
            relay_state=secrets.token_urlsafe(16),
            organization=organization,
            issued_at=moment,
            session_id=session_id,
            redirect_url=provider.sso_url,
        )
        self.pending.put(pending)
        return pending

    def authn_request_xml(self, pending: PendingLogin) -> str:
        """The `AuthnRequest` for a pending login, as XML."""
        provider = self.directory.for_organization(pending.organization)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pending.issued_at))
        return (
            '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID={quoteattr(pending.request_id)} Version="2.0" '
            f"IssueInstant={quoteattr(stamp)} "
            f"Destination={quoteattr(provider.sso_url)} "
            'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f"AssertionConsumerServiceURL={quoteattr(self.acs_url)}>"
            f"<saml:Issuer>{_escape(self.entity_id)}</saml:Issuer>"
            "</samlp:AuthnRequest>"
        )

    def organization_for_request(self, request_id: str) -> str:
        """Which organisation this request id was issued for."""
        return self.pending.organization_for(request_id)

    async def consume(
        self,
        raw: bytes,
        *,
        in_response_to: str,
        relay_state: str,
        session_id: str,
        ledger: Any,
        now: Any = None,
    ) -> Any:
        """Verify an assertion against **that organisation's** signers only.

        The organisation comes from the pending login, so the trust anchor is
        decided by the request this application issued rather than by the
        document answering it. That ordering is the whole defence against one
        customer's identity provider minting an identity in another's.

        Returns `wreath.saml.VerifiedAssertion`: facts, not a session. Signing
        anybody in is `JitProvisioning`'s job and a policy's after that.
        """
        from .saml import IdentityProvider, SamlRefusal, ServiceProvider, verify_response

        pending = self.pending.spend(
            in_response_to,
            relay_state=relay_state,
            session_id=session_id,
        )
        config = self.directory.for_organization(pending.organization)
        idp = IdentityProvider(entity_id=config.entity_id, certificates=config.certificates)
        sp = ServiceProvider(entity_id=self.entity_id, acs_url=self.acs_url)
        try:
            return await verify_response(
                raw,
                idp=idp,
                sp=sp,
                ledger=ledger,
                in_response_to=in_response_to,
                now=now,
            )
        except SamlRefusal as refusal:
            # Re-raised rather than propagated so a caller has one exception
            # type for the whole flow -- and the signature failure is named as
            # what it means here, because "no configured key verifies this" is
            # exactly the cross-organisation case when the keys are per
            # organisation.
            if refusal.reason in ("signature-unverified", "unknown-signer"):
                raise SsoRefusal(
                    "wrong-organisation-signer",
                    f"the assertion is not signed by a key that is a signer for "
                    f"{pending.organization!r}: {refusal}",
                ) from refusal
            raise SsoRefusal(refusal.reason, str(refusal)) from refusal


def _pem_body(certificate: str) -> str:
    """The base64 body of a PEM block, or the text unchanged if it is already bare."""
    if "-----" not in certificate:
        return certificate.strip()
    return "".join(
        line for line in certificate.splitlines() if not line.startswith("-----")
    ).strip()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True, slots=True)
class AttributeMapping:
    """Which assertion attributes become which fields. Declared, never inferred.

    A heuristic that reads `email` and misses `mail` is confident and wrong, and
    the failure is one duplicate account per user per identity provider. So an
    attribute this mapping does not name is refused rather than guessed at or
    dropped: a directory sending something unexpected is a configuration
    question, and answering it silently is how it stays unanswered.
    """

    email: str = "email"
    display_name: str | None = None
    external_id: str | None = None

    def declared(self) -> tuple[str, ...]:
        return tuple(
            name for name in (self.email, self.display_name, self.external_id) if name is not None
        )

    def apply(self, attributes: Mapping[str, Any]) -> dict[str, Any]:
        """Read the declared attributes out, refusing anything undeclared."""
        declared = set(self.declared())
        unknown = sorted(set(attributes) - declared)
        if unknown:
            raise SsoRefusal(
                "undeclared-attribute",
                f"the assertion carries {', '.join(unknown)}, which this mapping does "
                "not declare. Add it to AttributeMapping or stop the directory sending "
                "it -- an attribute nobody mapped is one nobody decided about.",
            )
        missing = [name for name in (self.email,) if name not in attributes]
        if missing:
            raise SsoRefusal(
                "missing-attribute",
                f"the assertion carries no {', '.join(missing)}, and this application "
                "identifies an account by email",
            )
        selected: dict[str, str | None] = {}
        for field_name, attribute_name in (
            ("email", self.email),
            ("display_name", self.display_name),
            ("external_id", self.external_id),
        ):
            key = cast(str, attribute_name)
            if key not in attributes:
                selected[field_name] = None
                continue
            value = attributes[key]
            if isinstance(value, str):
                candidate = value
            elif isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], str):
                candidate = value[0]
            else:
                raise SsoRefusal(
                    "attribute-cardinality",
                    f"the identity attribute {attribute_name!r} must carry exactly one text value",
                )
            if not candidate.strip():
                raise SsoRefusal(
                    "attribute-cardinality",
                    f"the identity attribute {attribute_name!r} must carry one "
                    "non-empty text value",
                )
            selected[field_name] = candidate
        return {
            "email": selected["email"],
            "display_name": selected["display_name"],
            "external_id": selected["external_id"],
        }


@dataclass(frozen=True, slots=True)
class ProvisionedLogin:
    """What a completed SSO login produced."""

    user_id: str
    organization: str
    membership: tuple[str, ...]
    #: `"pending"` when the organisation requires a second factor. A
    #: `SessionIdentityBackend` will not turn a pending session into an
    #: identity, which is what makes this compose with what already ships
    #: instead of bypassing it.
    session_state: Literal["authenticated", "pending"] = "authenticated"
    created: bool = False


class JitProvisioning:
    """Turn a verified assertion into an account and a membership.

    Both, together: an account with no membership sees nothing and reads as a
    bug in login, and a membership with no account cannot be signed in.

    **An existing account is adopted, never duplicated** -- the choice
    `scim_router`'s `POST /Users` already makes, for the same reason: somebody
    who signed up with a password before their company bought SSO keeps their
    data.
    """

    __slots__ = (
        "_accounts",
        "_memberships",
        "_revoke_sessions",
        "_roles",
        "_second_factor",
    )

    def __init__(
        self,
        *,
        roles: Iterable[str] = (),
        vocabulary: Iterable[str] = ("member", "admin"),
        require_second_factor: bool = False,
        revoke_sessions: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        if revoke_sessions is not None and not callable(revoke_sessions):
            raise TypeError("JIT revoke_sessions must be an async callable")
        if isinstance(roles, str) or isinstance(vocabulary, str):
            raise SsoRefusal(
                "invalid-role-configuration",
                "JIT roles and vocabulary must be iterables of complete role names, not text",
            )
        configured_roles = tuple(roles)
        configured_vocabulary = tuple(vocabulary)
        if any(
            not isinstance(role, str) or not role
            for role in (*configured_roles, *configured_vocabulary)
        ):
            raise SsoRefusal(
                "invalid-role-configuration",
                "JIT roles and vocabulary must contain only non-empty text role names",
            )
        if not isinstance(require_second_factor, bool):
            raise SsoRefusal(
                "invalid-second-factor",
                "JIT require_second_factor must be a boolean",
            )
        self._roles = configured_roles
        self._second_factor = require_second_factor
        self._revoke_sessions = revoke_sessions
        vocab = set(configured_vocabulary)
        # Checked here rather than at provisioning time: the roles are
        # configuration, and configuration that can only fail on somebody's
        # first login is configuration nobody tested.
        outside = sorted(set(self._roles) - vocab)
        if outside:
            raise SsoRefusal(
                "role-outside-vocabulary",
                f"{', '.join(outside)} is not in the declared role vocabulary "
                f"({', '.join(sorted(vocab))}). An identity-provider attribute is "
                "whatever a customer's directory admin typed, so it must not be able "
                "to name a role this application did not declare.",
            )
        self._accounts: dict[tuple[str, str], str] = {}
        self._memberships: dict[str, tuple[str, ...]] = {}

    def provision(self, *, organization: str, email: str) -> ProvisionedLogin:
        if (
            not isinstance(organization, str)
            or not organization.strip()
            or not isinstance(email, str)
            or not email.strip()
        ):
            raise SsoRefusal(
                "invalid-identity",
                "JIT provisioning requires a non-empty organisation and email identity",
            )
        key = (organization, email.strip().lower())
        user_id = self._accounts.get(key)
        created = user_id is None
        if user_id is None:
            user_id = f"user_{secrets.token_hex(8)}"
            self._accounts[key] = user_id
        self._memberships[user_id] = self._roles
        return ProvisionedLogin(
            user_id=user_id,
            organization=organization,
            membership=self._roles,
            session_state="pending" if self._second_factor else "authenticated",
            created=created,
        )

    async def revoke(self, *, organization: str, email: str) -> int:
        """De-provision. A revoked user holding a live cookie is why SSO was bought."""
        key = (organization, email.strip().lower())
        user_id = self._accounts.get(key)
        if user_id is None:
            return 0
        revoke_sessions = self._revoke_sessions
        if revoke_sessions is None:
            raise RuntimeError(
                "JIT deprovisioning requires a session revoker so live sessions cannot survive"
            )
        await revoke_sessions(user_id)
        self._accounts.pop(key, None)
        self._memberships.pop(user_id, None)
        return 1


@dataclass(frozen=True, slots=True)
class _OidcFlow:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    challenge: str
    organization: str
    session_id: str = field(repr=False)
    issued_at: float


class OidcRelyingParty:
    """The authorization-code flow, with PKCE, against one organisation's issuer.

    Three single-use values and each defends a different thing: `state` binds
    the callback to the browser that began (without it, an attacker's code
    redeemed in a victim's browser logs them into the attacker's account),
    `nonce` binds the id token to *this* authorization request, and the PKCE
    verifier binds the code redemption to this client.
    """

    __slots__ = (
        "_client_id",
        "_flows",
        "_issuer",
        "_keys",
        "_max_pending",
        "_next_sweep",
        "_pkce",
        "_ttl",
    )

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        pkce: Literal["S256", "plain"] = "S256",
        ttl: float = 600.0,
        max_pending: int = 10_000,
    ) -> None:
        if pkce != "S256":
            raise SsoRefusal(
                "weak-pkce",
                "PKCE method 'plain' sends the verifier as the challenge, so it "
                "protects nothing; only S256 is accepted",
            )
        try:
            parsed_issuer = urlsplit(issuer)
            _ = parsed_issuer.port
        except ValueError as error:
            raise SsoRefusal(
                "insecure-issuer",
                "OIDC issuer must be an absolute HTTPS URL without credentials, "
                "a query, or a fragment",
            ) from error
        if (
            any(ord(character) < 0x21 or 0x7F <= ord(character) <= 0x9F for character in issuer)
            or parsed_issuer.scheme != "https"
            or parsed_issuer.hostname is None
            or parsed_issuer.username is not None
            or parsed_issuer.port == 0
            or parsed_issuer.query
            or parsed_issuer.fragment
            or "\\" in issuer
        ):
            raise SsoRefusal(
                "insecure-issuer",
                "OIDC issuer must be an absolute HTTPS URL without credentials, "
                "a query, or a fragment",
            )
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not isfinite(ttl)
            or ttl <= 0
        ):
            raise ValueError("OIDC state ttl must be a positive finite number")
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < 1:
            raise ValueError("OIDC max_pending must be a positive integer of at least one")
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._pkce = pkce
        self._ttl = ttl
        self._max_pending = max_pending
        self._flows = CapabilityMap(
            max_entries=max_pending,
            ttl=ttl,
            clock=time.time,
            overflow="refuse",
            expire_at_deadline=False,
        )
        self._next_sweep = float("inf")
        #: `kid -> key`, filled by `refresh()` at startup. Never on the request
        #: path: a fetch driven by a request lets an unauthenticated caller aim
        #: an outbound request, and puts the issuer's outage in front of every
        #: login rather than in front of the refresh.
        self._keys: dict[str, Any] = {}

    @property
    def fetches_on_request_path(self) -> bool:
        """Always `False`, and asserted rather than promised."""
        return False

    async def refresh(self, fetch: Any) -> int:
        """Reload discovery and JWKS. Called at lifespan startup, never per request."""
        document = await fetch(f"{self._issuer}/.well-known/openid-configuration")
        if not isinstance(document, Mapping):
            raise SsoRefusal("invalid-discovery", "OIDC discovery must be a JSON object")
        if document.get("issuer") != self._issuer:
            raise SsoRefusal(
                "issuer-mismatch", "OIDC discovery issuer does not match the configured issuer"
            )
        from ._auth.oidc import _require_same_origin

        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise SsoRefusal(
                "invalid-discovery",
                "OIDC discovery must name a non-empty jwks_uri",
            )
        try:
            _require_same_origin(self._issuer, jwks_uri)
        except ValueError as error:
            raise SsoRefusal(
                "endpoint-origin-mismatch",
                "OIDC JWKS endpoint is not on the configured issuer origin",
            ) from error
        keys = await fetch(jwks_uri)
        if not isinstance(keys, Mapping):
            raise SsoRefusal("invalid-jwks", "OIDC JWKS must be a JSON object")
        raw_keys = keys.get("keys", ())
        if not isinstance(raw_keys, (list, tuple)) or len(raw_keys) > _MAX_JWKS_KEYS:
            raise SsoRefusal(
                "invalid-jwks",
                f"OIDC JWKS keys must be a list of at most {_MAX_JWKS_KEYS} entries",
            )
        loaded: dict[str, Any] = {}
        for key in raw_keys:
            if not isinstance(key, Mapping):
                raise SsoRefusal("invalid-jwks", "each OIDC JWKS key must be a JSON object")
            kid = key.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
                raise SsoRefusal(
                    "invalid-jwks",
                    "each OIDC JWKS key needs a non-empty kid of at most "
                    f"{_MAX_KID_LENGTH} characters",
                )
            if kid in loaded:
                raise SsoRefusal(
                    "duplicate-key-id",
                    f"OIDC JWKS carries duplicate key id {kid!r}; a kid must select "
                    "exactly one verification key",
                )
            copied = dict(key)
            for name in ("key_ops", "x5c"):
                if isinstance(copied.get(name), list):
                    copied[name] = tuple(copied[name])
            loaded[kid] = MappingProxyType(copied)
        self._keys = loaded
        return len(self._keys)

    def begin_login(
        self,
        *,
        organization: str,
        session_id: str,
        now: float | None = None,
    ) -> _OidcFlow:
        if not isinstance(session_id, str) or not session_id:
            raise SsoRefusal(
                "session-binding-required",
                "OIDC login requires a non-empty browser session binding",
            )
        moment = time.time() if now is None else now
        self._sweep_flows(moment)
        if len(self._flows) >= self._max_pending:
            raise SsoRefusal(
                "pending-capacity",
                f"the OIDC pending-login store is at its ceiling of {self._max_pending}",
            )
        verifier = secrets.token_urlsafe(64)
        challenge = (
            urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        flow = _OidcFlow(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(16),
            verifier=verifier,
            challenge=challenge,
            organization=organization,
            session_id=session_id,
            issued_at=moment,
        )
        if not self._flows.put(flow.state, flow, now=moment):
            raise SsoRefusal(
                "pending-capacity",
                f"the OIDC pending-login store is at its ceiling of {self._max_pending}",
            )
        self._next_sweep = self._flows.next_deadline
        return flow

    def consume_state(self, state: str, *, session_id: str, now: float | None = None) -> _OidcFlow:
        """Spend the state, refusing a replay or another browser's callback."""
        flow = self._flows.held(state)
        if flow is None:
            raise SsoRefusal(
                "unknown-state",
                "this callback carries a state this application did not issue, or "
                "already spent. A state that is not single-use is CSRF on the login "
                "endpoint.",
            )
        self._flows.discard(state)
        moment = time.time() if now is None else now
        if (
            isinstance(moment, bool)
            or not isinstance(moment, (int, float))
            or not isfinite(moment)
            or moment < flow.issued_at
        ):
            raise SsoRefusal(
                "invalid-time",
                "the clock is invalid or earlier than when this OIDC state was issued",
            )
        if moment - flow.issued_at > self._ttl:
            raise SsoRefusal(
                "expired-state",
                f"this OIDC state expired after its {self._ttl:g}s lifetime",
            )
        if not isinstance(session_id, str) or not session_id:
            raise SsoRefusal(
                "session-binding-required",
                "OIDC callback requires a non-empty browser session binding",
            )
        if not hmac.compare_digest(flow.session_id, session_id):
            raise SsoRefusal(
                "state-session-mismatch",
                "this callback's state was issued to a different browser session; an "
                "attacker's code redeemed in a victim's browser signs them into the "
                "attacker's account",
            )
        return flow

    def _sweep_flows(self, now: float) -> None:
        if now <= self._next_sweep:
            return
        self._flows.sweep(now=now)
        self._next_sweep = self._flows.next_deadline

    def check_nonce(self, claims: Mapping[str, Any], *, expected_nonce: str) -> None:
        """The claim that binds an id token to one authorization request."""
        actual = claims.get("nonce")
        matches = (
            isinstance(expected_nonce, str)
            and bool(expected_nonce)
            and isinstance(actual, str)
            and hmac.compare_digest(actual, expected_nonce)
        )
        if not matches:
            raise SsoRefusal(
                "nonce-mismatch",
                "the id token's nonce does not match the one this login issued, so it "
                "is not an answer to this authorization request",
            )

    def key_for(self, kid: str) -> Any:
        """The signing key for a `kid`, or refuse. **Never fetches.**"""
        if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
            raise SsoRefusal(
                "invalid-key-id",
                f"an OIDC key id must be non-empty text of at most {_MAX_KID_LENGTH} characters",
            )
        key = self._keys.get(kid)
        if key is None:
            raise SsoRefusal(
                "unknown-key",
                f"no signing key {kid!r} is loaded; keys refresh at startup and are "
                "never fetched on the request path, so an unknown key id is simply "
                "unverified rather than a reason to call the issuer",
            )
        return key
