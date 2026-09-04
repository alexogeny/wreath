"""The OAuth 2.1 resource-server half: metadata, the challenge, and audience.

MCP's authorization model makes a remote server an OAuth 2.1 **resource
server** and nothing else. It does not issue tokens, it does not register
clients, and it does not run a login flow -- it publishes where its tokens come
from, and then refuses every token that was not minted for it.

Three pieces, and the third is the one that matters:

1. `/.well-known/oauth-protected-resource` (RFC 9728) says which authorization
   servers this endpoint trusts. It is a small JSON document, served
   unauthenticated, because a client that cannot read it cannot get a token.
2. A `401` carries a Bearer challenge naming that document, so a client that
   arrives with nothing learns where to go instead of guessing.
3. **Audience binding.** A token is verified, and then its `aud` claim is
   checked against this resource's identifier. This is the whole reason the
   protected-resource spec exists: without it, a token a user was persuaded to
   mint for *some other* MCP server is a valid token here, and the confused
   deputy the model is holding becomes an authenticated one. `MCPAuth` performs
   that check itself rather than trusting the verifier to have been configured
   with the right `audience=`, because a deployment that got that wrong would
   have no symptom until someone exploited it.

Dynamic client registration is deliberately absent. It belongs to the
authorization server a deployment runs, not to the resource server -- see the
guide.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .._auth.backends import BearerTokenBackend, Verifier
from .._auth.models import Identity
from .._auth.oauth2 import bearer_challenge
from ..request import Request


class Unauthenticated(Exception):
    """A request carried no usable token. Rendered as a 401 with a challenge.

    `error` is `None` when nothing was presented at all, which RFC 6750 §3.1
    distinguishes from a token that was presented and rejected.
    """

    __slots__ = ("description", "error")

    def __init__(self, error: str | None, description: str | None = None) -> None:
        super().__init__(description or error or "unauthenticated")
        self.error = error
        self.description = description


def _require_https_url(value: object, label: str) -> None:
    message = (
        f"MCPAuth {label} {value!r} must be an absolute HTTPS URL without "
        "credentials, controls, or fragment"
    )
    if not isinstance(value, str):
        raise ValueError(message)
    if any(ord(character) <= 0x20 or 0x7F <= ord(character) <= 0x9F for character in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.hostname is not None:
            parsed.hostname.encode("idna")
    except (UnicodeError, ValueError) as error:
        raise ValueError(message) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or port == 0
        or parsed.fragment
    ):
        raise ValueError(message)


def _audience_of(claims: Any) -> frozenset[str]:
    """The `aud` claim as a set. A malformed claim is an empty set, never a match."""
    if not hasattr(claims, "get"):
        return frozenset()
    value = claims.get("aud")
    if isinstance(value, str):
        return frozenset((value,))
    if isinstance(value, (list, tuple)):
        return frozenset(entry for entry in value if isinstance(entry, str))
    return frozenset()


@dataclass(frozen=True, slots=True)
class MCPAuth:
    """What an MCP endpoint publishes about itself, and what it will accept.

    Attributes:
        resource: This endpoint's canonical identifier -- the absolute URL a
            client asks the authorization server for a token *for*, and the
            value it must find in the token's `aud`. It should be the public URL
            of the MCP endpoint itself: `https://api.example.com/mcp`.
        authorization_servers: Issuer identifiers this endpoint trusts. Purely
            advisory: they tell a client where to go, and change nothing about
            what is accepted here, which is decided by `verifier` and `audience`.
        verifier: The token verifier -- a `JwtVerifier`, an `OidcProvider`'s
            `bearer_verifier()`, or any callable taking the compact token and
            returning an `Identity` or `None`. Without one, every request is
            refused: an endpoint that publishes metadata and then accepts
            anything is worse than one with no metadata at all.
        audience: The value a token's `aud` must contain. Defaults to
            `resource`, which is what RFC 8707 asks an authorization server to
            put there; set it only when the deployment's tokens name the
            resource by some other identifier.
        scopes_supported: Scopes a client may usefully request. Advertised only.
        resource_name: A human-readable name for the resource, for a consent
            screen that is deciding what to say about it.
        resource_documentation: A URL a developer can read.

    Raises:
        ValueError: `resource` is empty, or no authorization server is named.
    """

    resource: str
    authorization_servers: Sequence[str] = ()
    verifier: Verifier | None = None
    audience: str | None = None
    scopes_supported: Sequence[str] = ()
    resource_name: str | None = None
    resource_documentation: str | None = None
    _backend: BearerTokenBackend | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.resource:
            raise ValueError(
                "MCPAuth needs a `resource=`: the absolute URL of this MCP "
                "endpoint. It is what a client asks for a token for, and what "
                "that token's `aud` claim must name."
            )
        _require_https_url(self.resource, "resource")
        authorization_servers = tuple(self.authorization_servers)
        if not authorization_servers:
            raise ValueError(
                "MCPAuth needs at least one `authorization_servers=` entry. A "
                "protected-resource document that names none tells a client it "
                "must authenticate and gives it nowhere to do so."
            )
        for authorization_server in authorization_servers:
            _require_https_url(authorization_server, "authorization server")
        if self.verifier is not None:
            # A frozen dataclass, so the compiled backend goes in the same way
            # every other derived field would.
            object.__setattr__(self, "_backend", BearerTokenBackend(self.verifier))

    @property
    def expected_audience(self) -> str:
        """The value a token's `aud` must contain to be accepted here.

        `audience` when one was set, and `resource` otherwise — which is what
        RFC 8707 asks an authorization server to put there. This is the check
        that makes a token minted for another resource useless at this one, so
        it is never empty: `resource` is required at construction.
        """
        return self.audience if self.audience is not None else self.resource

    def document(self) -> dict[str, Any]:
        """The RFC 9728 protected-resource metadata for this endpoint."""
        metadata: dict[str, Any] = {
            "resource": self.resource,
            "authorization_servers": list(self.authorization_servers),
            # Only the `Authorization` header is read. RFC 6750 also defines a
            # form field and a query parameter; a token in a query string ends
            # up in access logs and referrers, and MCP forbids both.
            "bearer_methods_supported": ["header"],
        }
        if self.scopes_supported:
            metadata["scopes_supported"] = list(self.scopes_supported)
        if self.resource_name is not None:
            metadata["resource_name"] = self.resource_name
        if self.resource_documentation is not None:
            metadata["resource_documentation"] = self.resource_documentation
        return metadata

    def challenge(self, metadata_url: str, *, error: str | None, description: str | None) -> bytes:
        """The `WWW-Authenticate` value for a 401 from this endpoint.

        Always carries `resource_metadata=<metadata_url>` (RFC 9728 §5.3), which
        is how a client that has never seen this server discovers which
        authorization server to get a token from — a 401 without it says a token
        is needed and nothing about where to obtain one.

        Args:
            metadata_url: The absolute URL of this endpoint's
                protected-resource metadata document.
            error: The RFC 6750 error code, or `None`. `None` is meaningful
                rather than lazy: a request that carried no credentials at all
                gets a bare challenge, because there is no token for an error
                code to describe.
            description: Human-readable detail, or `None` to omit it.
        """
        return bearer_challenge(
            error=error, description=description, resource_metadata=metadata_url
        )

    async def authenticate(self, request: Request) -> Identity:
        """Verify the request's bearer token and bind it to this resource.

        Raises:
            Unauthenticated: No token, an unverifiable one, or one minted for a
                different resource.
        """
        backend = self._backend
        if backend is None:
            raise Unauthenticated(
                "invalid_token",
                "this MCP endpoint is protected but has no token verifier configured",
            )
        try:
            authorization = request._single_header(b"authorization")
        except ValueError:
            raise Unauthenticated(
                "invalid_token", "the authorization header must occur exactly once"
            ) from None
        if authorization is None:
            raise Unauthenticated(None)
        if b"," in authorization:
            raise Unauthenticated(
                "invalid_token", "the authorization header cannot contain multiple credentials"
            )
        identity = await backend.authenticate(request)
        if identity is None:
            raise Unauthenticated("invalid_token", "the bearer token could not be verified")
        expected = self.expected_audience
        if expected not in _audience_of(identity.claims):
            # The confused-deputy case. The signature was good and the issuer
            # was trusted; the token simply was not minted for this server, and
            # replaying it here is exactly the attack RFC 8707 and RFC 9728
            # exist to stop.
            raise Unauthenticated(
                "invalid_token",
                f"the token's audience does not include {expected!r}",
            )
        return identity


def metadata_path_for(path: str) -> str:
    """The RFC 9728 §3 well-known URL path for a resource served at `path`.

    The well-known segment is *inserted between the host and the path* rather
    than appended, so an endpoint at `/mcp` publishes at
    `/.well-known/oauth-protected-resource/mcp`. Two MCP servers on one host
    therefore get two metadata documents instead of silently sharing one.
    """
    if path in ("", "/"):
        return "/.well-known/oauth-protected-resource"
    return "/.well-known/oauth-protected-resource" + (path if path.startswith("/") else "/" + path)


__all__ = ["MCPAuth", "Unauthenticated", "metadata_path_for"]
