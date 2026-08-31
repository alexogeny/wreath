"""HTTP Message Signatures (RFC 9421), in both directions, at the boundary.

One module, because signing and verifying share the *signature base* -- the
canonical byte string both sides hash -- and two implementations of that string
disagreeing is where every interoperability failure in this protocol comes
from. `_signature_base` is written once and called by both.

```python
from wreath import Wreath
from wreath.signatures import Signatures

app = Wreath(signatures=Signatures(
    directories=("https://openai.com/.well-known/http-message-signatures-directory",),
    max_age=60.0,
))
```

**The boundary establishes a fact; the policy set decides what it means.** This
module never allows or denies. Verification publishes `signature_verified` and
`signature_agent` for `Signatures.cedar_context`, and a Cedar policy says what
an unverified caller may reach:

    permit (principal, action == Action::"read", resource is Article)
    unless { context.signature_verified == false && resource.paid };

**Verified is not trusted.** A valid signature proves *which* agent sent the
request. It says nothing about whether that agent is welcome, and conflating the
two rebuilds the `User-Agent` allow-list with extra steps and more code.

## What "this request" means, exactly

A signature covers the components the caller listed, and **anything it does not
list rides along unauthenticated.** That is the protocol working as designed,
and it is why the required set here is not the minimum the RFC permits:

* `@method`, `@authority`, `@path` and **`@query`** are demanded of every
  signature. Without the query the signature is over an *endpoint*, so `?x=1`
  and `?admin=1` produce identical bases and anyone who observes one signed
  request replays it with the parameters of their choosing.
* **`content-digest` is demanded of any request that carries a body**, and the
  digest is *recomputed from the bytes*. Covering the header alone is worth
  nothing: it canonicalizes as a string like any other header, so a signature
  over it proves only that the sender typed it. The body has not arrived when
  this middleware runs, so the expectation is parked on `request.state` and
  `Request.body()`/`Request.stream()` spend it -- a mismatch is a 400 there.

`SignatureFacts.covered` and the `signature_covered` context key publish the
component list, so a policy can require an end-to-end signature
(`context.signature_covered.contains("content-digest")`) instead of reading
`signature_verified` and assuming what it covered.

## What it does not do on the request path

**It never fetches a key.** Key material comes from a directory refreshed off
the request path (lifespan startup, then `refresh()` on whatever schedule the
deployment chooses). A signature naming a key this process has not got is simply
unverified -- the fact is absent and the policy decides, exactly as for a request
that carried no signature at all.

That is a deliberate refusal, not an omission. Resolving an unknown `keyid` by
fetching turns one inbound request from an unauthenticated crawler into one
outbound request to a host the crawler named, which is an amplifier with a
signature header for a trigger.

It is also why this does **not** reuse `wreath._auth.jwks.JwksCache`, which is
otherwise the obvious candidate. That class refreshes *on an unknown kid* --
correct for a JWT verifier, whose callers hold a token from a provider it
already trusts, and exactly the amplifier here, whose callers are anonymous.
`JwksCache` guards it with a single-flight lock and a negative cache; this
module does not need a guard because it does not have the behaviour. What is
reused is the parsing: `wreath._auth.jwt.key_from_jwk` reads the JWK and
`wreath._auth._ecverify.verify_ed25519` checks the signature, so there is no
second JWK reader and no second Ed25519 implementation in the tree.

## Profile versioning

Web Bot Auth is an application profile of RFC 9421, standardised through an IETF
working group chartered in 2026, with documents still moving through 2026-27.
`Signatures(profile=...)` names which profile is being enforced and
`WEB_BOT_AUTH_2026` is the only one implemented; the well-known directory path
is expected to change, so it is a constant here rather than a literal spread
through the code.

## Relationship to `wreath.webhooks`

Both sign and verify HTTP requests, and they are deliberately *not* one profile.
`webhooks` is a symmetric HMAC over an exact body with wreath's own versioned
base and a key id chosen by the sender; this is asymmetric Ed25519 over a
component list the caller declares per request. Neither base can be expressed in
the other's terms, so folding them into one signer would produce a function with
two disjoint halves and a mode flag.

Their replay ledgers stay separate too, and that one *is* a near-miss worth
naming: `webhooks.LocalReplayStore` and `NonceLedger` are both bounded TTL claim
ledgers. They differ in the property that matters -- a full store evicts there
and refuses here (see `NonceLedger` for why) -- and in shape: one is `async` for
a claim taken under a lock, this one is synchronous because it runs on the
ingress path where an `await` is not available. Merging them would hand one
caller a lock it cannot take or the other a race it does not have. The shared
part that *was* extractable already exists and is used: `wreath.kv.KV` is the
bounded, lazily-expiring table underneath this one. `LocalReplayStore` predates
it and still hand-rolls a heap; converting it would change webhook eviction
order, which is a behaviour change for a live subsystem and belongs in its own
change with its own measurement.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, quote

from ._auth.jwt import JwtError, OkpPublicKey, key_from_jwk
from ._capability_map import CapabilityMap
from ._native import _core
from ._reqcache import resolve_once
from .digest import SUPPORTED_DIGEST_ALGORITHMS, Digest, DigestError, _checksum
from .exceptions import HTTPException
from .state import BODY_CHECK_SLOT
from .temporal import Duration

__all__ = [
    "WEB_BOT_AUTH_2026",
    "NonceLedger",
    "PaymentRequired",
    "SignatureError",
    "SignatureFacts",
    "SigningKey",
    "Signatures",
    "crawler_policy",
    "llms_txt",
    "robots_txt",
    "sign_request",
    "signature_base",
]

#: The Web Bot Auth profile this module implements. Named rather than assumed,
#: because the IETF work is live: the directory path, the required component
#: set, and the `tag` value are all things a later profile may move. A
#: deployment pins one; a `Signatures` built for an unknown profile refuses at
#: construction rather than verifying under rules nobody chose.
WEB_BOT_AUTH_2026: Final = "web-bot-auth-2026"

#: Where a Web Bot Auth operator publishes its keys. Expected to change.
DIRECTORY_PATH: Final = "/.well-known/http-message-signatures-directory"

#: The only signature algorithm this module verifies. Web Bot Auth mandates
#: Ed25519, and an allow-list of exactly one is the strongest available answer
#: to algorithm confusion: there is no second algorithm to be confused with.
#: `alg` in a signature is *advisory* -- the key's own family decides, so a
#: caller cannot talk this into a cheaper check by relabelling their signature.
_ALGORITHMS: Final = frozenset({"ed25519"})

#: Components a signature must cover to be worth anything. A signature over
#: `date` alone verifies fine and is replayable against every endpoint on the
#: host; binding method, authority and path is what makes it a signature over
#: *this* request. Refusing rather than accepting-and-noting, because a caller
#: that omits them has not made a weaker claim, it has made a different one.
#:
#: `@query` is in the set because leaving it out makes this a signature over an
#: **endpoint** rather than a request: `?x=1` and `?admin=1` produce the same
#: base, so anyone who observes one signed request replays it against any query
#: string they like. RFC 9421 §2.2.7 covers the absolute query including the
#: leading `?`, so a request with no query is still distinguishable from one
#: with an empty one.
_REQUIRED_COMPONENTS: Final = ("@method", "@authority", "@path", "@query")

#: The component that covers the body, and the other half of the same hole.
#: Required of any request that carries one -- see `_BODY_METHODS` below.
_DIGEST_COMPONENT: Final = "content-digest"

#: `Content-Digest` algorithms this module will check, per RFC 9530 §3. An
#: allow-list rather than a lookup into `hashlib`: `sha-1` and `md5` are
#: registered spellings, and accepting one would let a caller choose a digest
#: nobody should be relying on for integrity.
_DIGEST_ALGORITHMS: Final = SUPPORTED_DIGEST_ALGORITHMS

#: Where the deferred body check waits between ingress and the first read of the
#: body. See `Signatures._verify_headers` and `Request._check_body`.
_DIGEST_SLOT: Final = BODY_CHECK_SLOT

#: Ceiling on the two signature headers together. They are attacker-supplied and
#: parsed before anything is verified, so the parse has to be bounded by
#: something other than good intentions.
_MAX_HEADER_BYTES: Final = 8 * 1024

#: Ceiling on covered components in one signature. RFC 9421 sets none; a list of
#: ten thousand `@query-param`s is a parse amplifier without one.
_MAX_COMPONENTS: Final = 64

#: Where this request's verification outcome lives for the rest of the request.
_FACTS_SLOT: Final = "_signature_facts"


class SignatureError(Exception):
    """A signature was present and did not verify.

    Raised by the signing and verifying helpers. The middleware never lets one
    escape: at ingress a failed verification is *absence of the fact*, not a
    refusal, because refusing is the policy set's job.
    """


# Structured fields -- the bounded subset RFC 9421 needs (RFC 8941)
# Not a general RFC 8941 implementation, and deliberately not: a general one is
# a library, and the two headers here need a dictionary whose values are an
# inner list of strings with parameters, and a dictionary whose values are byte
# sequences. Anything outside that is refused rather than tolerated, because
# tolerating an unparsed construct means verifying a different string from the
# one the sender signed.


def _parse_string(text: str, index: int) -> tuple[str, int]:
    return _core.signature_parse_string(text, index, SignatureError)


def _parse_dictionary(text: str, *, inner_list: bool) -> dict[str, Any]:
    """A structured dictionary whose values are inner lists, or bare items.

    Both signature headers are dictionaries keyed by a caller-chosen label, and
    the two must be read with the same key set -- which is why one parser serves
    both and the labels are compared rather than assumed.
    """
    return _core.signature_parse_dictionary(
        text, inner_list, SignatureError, _MAX_HEADER_BYTES, _MAX_COMPONENTS
    )


def _verification_plan(raw_input: bytes, raw_signature: bytes) -> Any:
    return _core.signature_compile_pair(
        raw_input.decode("latin-1"),
        raw_signature.decode("latin-1"),
        SignatureError,
        _MAX_HEADER_BYTES,
        _MAX_COMPONENTS,
    )


def _serialize_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _serialize_bare(value: Any) -> str:
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, bytes):
        return ":" + base64.b64encode(value).decode("ascii") + ":"
    if isinstance(value, int):
        return str(value)
    raise SignatureError(f"cannot serialize parameter of type {type(value).__name__}")


def _serialize_params(params: Mapping[str, Any]) -> str:
    out: list[str] = []
    for key, value in params.items():
        if value is True:
            out.append(f";{key}")
        else:
            out.append(f";{key}={_serialize_bare(value)}")
    return "".join(out)


def _serialize_component(name: str, params: Mapping[str, Any]) -> str:
    return _serialize_string(name) + _serialize_params(params)


# The signature base -- written once, called by both directions


@dataclass(frozen=True, slots=True)
class RequestMessage:
    """The parts of a request a signature can cover.

    A plain value rather than a `Request`, so the same base builder serves
    verification of an inbound request and signing of an outbound one -- and so
    the RFC's own test vectors can be driven through it directly.
    """

    method: str
    scheme: str
    authority: str
    path: str
    query: bytes = b""
    headers: Mapping[bytes, bytes] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        value = self.headers.get(name.encode("ascii"))
        return None if value is None else value.decode("latin-1")


def _derived(name: str, params: Mapping[str, Any], message: RequestMessage) -> str:
    if name == "@method":
        return message.method.upper()
    if name == "@authority":
        # RFC 9421 §2.2.3: lowercased, with the scheme's default port omitted.
        # Refused rather than canonicalized to "" when the request carries no
        # authority at all: an empty authority would let a signature minted for
        # one host verify against any other host that also failed to send one,
        # which is the opposite of what covering `@authority` is for.
        if not message.authority:
            raise SignatureError("request has no authority to cover")
        authority = message.authority.lower()
        default = ":443" if message.scheme.lower() == "https" else ":80"
        return authority.removesuffix(default)
    if name == "@scheme":
        return message.scheme.lower()
    if name == "@path":
        return message.path
    if name == "@query":
        # RFC 9421 §2.2.7: the absolute query including the leading "?"; a
        # request with no query still covers "?" so that "no query" and "empty
        # query" cannot be signed as the same message.
        return "?" + message.query.decode("latin-1")
    if name == "@request-target":
        query = message.query.decode("latin-1")
        return message.path + ("?" + query if query else "")
    if name == "@target-uri":
        query = message.query.decode("latin-1")
        return f"{message.scheme.lower()}://{message.authority}{message.path}" + (
            "?" + query if query else ""
        )
    if name == "@query-param":
        wanted = params.get("name")
        if not isinstance(wanted, str):
            raise SignatureError("@query-param requires a name parameter")
        pairs = parse_qsl(message.query.decode("latin-1"), keep_blank_values=True)
        found = [value for key, value in pairs if key == wanted]
        if len(found) != 1:
            # Zero is a component that cannot be built; more than one is
            # ambiguous, and RFC 9421 says an ambiguous parameter is an error
            # rather than a first-wins guess.
            raise SignatureError(f"@query-param {wanted!r} is absent or repeated")
        return quote(found[0], safe="")
    raise SignatureError(f"unsupported derived component {name!r}")


def signature_base(
    message: RequestMessage,
    components: Sequence[tuple[str, Mapping[str, Any]]],
    params: Mapping[str, Any],
) -> bytes:
    """The canonical bytes a signature covers, per RFC 9421 §2.5.

    One line per covered component -- the quoted, lowercased identifier with its
    parameters, then `": "`, then the value -- and a final `"@signature-params"`
    line carrying the serialized component list and the signature parameters.
    Lines are joined with `\\n` and there is no trailing newline.

    Pinned against the RFC's own Ed25519 vector (§B.2.6) in
    `tests/test_signatures_base.py`, byte for byte, which is the only way to
    know a base builder agrees with anyone else's.

    Raises:
        SignatureError: An unknown component, an unknown component parameter, a
            covered header the message does not carry, or a duplicated component.
    """
    return _core.signature_base(
        message,
        components,
        params,
        SignatureError,
        _derived,
        _MAX_COMPONENTS,
    )


# The body -- covered by reference, checked when it arrives


def _carries_body(headers: Mapping[bytes, bytes]) -> bool:
    """Whether this request has a body a signature would have to cover.

    Read off the framing headers rather than the method, because the method is
    not what decides: a `DELETE` may carry a body and a `POST` may not. A caller
    who *adds* a body to a bodyless signed request has to add the framing for it
    too, which is what makes this the honest test rather than a heuristic.
    """
    length = headers.get(b"content-length")
    if length is not None and length.strip() not in (b"", b"0"):
        return True
    return headers.get(b"transfer-encoding") is not None


def _digest_expectation(raw: bytes | None) -> tuple[str, bytes]:
    """The one algorithm and digest a covered `Content-Digest` commits to.

    RFC 9530 §3: a structured dictionary of byte sequences keyed by algorithm.
    A sender may list several; this takes the strongest it recognises and
    refuses a header that names none of them, because a digest nobody checks is
    the defect this exists to close.
    """
    if raw is None:
        raise SignatureError("covered header 'content-digest' is not present")
    try:
        return Digest.parse(raw).expectation()
    except DigestError as error:
        raise SignatureError(str(error)) from error


def _digest(algorithm: str, body: bytes) -> bytes:
    return _checksum(algorithm, body)


# Replay


class NonceLedger:
    """Bounded single-use nonce memory that **refuses when it is full**.

    The one semantic difference from `webhooks.LocalReplayStore`, and the reason
    this is not that class with a flag. A webhook sender has already proved a
    shared secret before its event id reaches the replay store, so evicting
    under load costs a duplicate delivery from a party you authenticated. Here
    the nonce space is controlled by an *unauthenticated* caller: eviction under
    load is an attacker flushing the ledger and replaying whatever it displaced,
    which is the exact attack the ledger exists to stop.

    So a full ledger refuses the request instead. That is a denial-of-service
    trade made deliberately in the safe direction, it is *counted* rather than
    silent (`refusals`), and `wreath doctor` has a number to look at. Size
    `max_entries` for the burst you expect.

    **One worker's memory.** Two workers each remember their own nonces, so a
    replay routed to the other worker is not caught. That is a fast path, not a
    guarantee, and it is why the timestamp window is the primary bound and this
    is the secondary one.

    Args:
        max_entries: Nonces retained. Reaching it refuses rather than evicts.
        ttl: Seconds a nonce is remembered. Should be at least `max_age`, or a
            replay is neither too old nor remembered.

    Raises:
        ValueError: Either bound is non-positive.
    """

    __slots__ = ("_table", "refusals", "replays")

    def __init__(self, *, max_entries: int = 16384, ttl: Any = 300.0) -> None:
        ttl = Duration.of(ttl).total_seconds()
        if max_entries < 1:
            raise ValueError("nonce ledger max_entries must be positive")
        if ttl <= 0:
            raise ValueError("nonce ledger ttl must be positive")
        self._table = CapabilityMap(max_entries=max_entries, ttl=ttl, overflow="refuse")
        #: Requests refused because the ledger was full. A rising number is a
        #: flood or an undersized ledger, and both want a human.
        self.refusals = 0
        #: Nonces seen twice. The ledger doing its job.
        self.replays = 0

    @property
    def size(self) -> int:
        """Live nonces, expired ones excluded."""
        return len(self._table)

    def seen(self, nonce: str, *, now: float | None = None) -> bool:
        """Whether `nonce` is already spent. Records nothing, counts a replay.

        The read half of `claim`, split out so a verifier can refuse a replay
        before paying for a signature check without letting an unverified caller
        write to the ledger. A full ledger is **not** a hit here: refusing on
        fullness is `claim`'s job, and doing it in the lookup would restore the
        exhaustion this split exists to remove.
        """
        if self._table.peek(nonce, now=now) is None:
            return False
        self.replays += 1
        return True

    def claim(self, nonce: str, *, now: float | None = None) -> bool:
        """Whether `nonce` is fresh. Records it when it is.

        Returns:
            True if this is the first time; False if it was seen, or if the
            ledger is full -- the caller cannot tell those apart, and must not,
            because both mean "this request is not verified".
        """
        table = self._table
        if table.peek(nonce, now=now) is not None:
            self.replays += 1
            return False
        if not table.claim(nonce, now=now):
            # Fail closed. The capability table's refusal policy prevents the
            # native KV from evicting a spent nonce to make room.
            self.refusals += 1
            return False
        return True


# Keys


@dataclass(frozen=True, slots=True)
class SigningKey:
    """A private Ed25519 key, for the outbound direction.

    `sign` is supplied by the caller rather than implemented here: wreath's
    zero-dependency crypto is *verify-only* (`wreath._auth._ecverify` says so in
    its first paragraph, and the reason is that verification has no nonce and no
    private key to get wrong). A deployment that signs outbound requests has a
    signing key somewhere already -- in `cryptography`, in an HSM, in a KMS --
    and this takes the callable it already has.

    Args:
        key_id: The `keyid` parameter receivers will see, and look up.
        sign: `bytes -> bytes`, producing a 64-byte Ed25519 signature.
        agent: The `Signature-Agent` value, i.e. where this key is published.
    """

    key_id: str
    sign: Callable[[bytes], bytes]
    agent: str | None = None


class _Directory:
    """One operator's published keys, replaced wholesale off the request path.

    Deliberately not a cache: there is no lookup-triggered fetch, no negative
    cache and no single-flight lock, because there is no fetch to guard. The
    module docstring says why, and why `wreath._auth.jwks.JwksCache` -- which
    has all three, and needs them -- is the wrong thing to reuse here.

    Keys are parsed by `wreath._auth.jwt.key_from_jwk`, which is the tree's one
    JWK reader.
    """

    __slots__ = ("_keys", "origin", "path", "url")

    def __init__(self, url: str) -> None:
        if not url.startswith("https://"):
            # An unauthenticated origin publishing verification keys makes the
            # whole signature meaningless: whoever can rewrite the response can
            # mint keys. There is no development exception, because the
            # development exception is what ships.
            raise ValueError("signature directories must be https")
        self.url = url
        scheme, _, rest = url.partition("://")
        host, _, path = rest.partition("/")
        self.origin = f"{scheme}://{host}"
        self.path = "/" + path
        self._keys: dict[str, OkpPublicKey] = {}

    def key(self, key_id: str) -> OkpPublicKey | None:
        """The named key, or None. Never fetches; see the module docstring."""
        return self._keys.get(key_id)

    def install(self, document: Mapping[str, Any]) -> int:
        """Replace this directory's keys from a parsed JWKS document.

        Returns the number of usable keys. A malformed or non-Ed25519 entry is
        skipped rather than failing the whole document -- an operator rotating
        in a key type this profile does not verify should not blind us to the
        keys it does.
        """
        keys: dict[str, OkpPublicKey] = {}
        for jwk in document.get("keys", ()):
            if not isinstance(jwk, Mapping):
                continue
            key_id = jwk.get("kid")
            if not isinstance(key_id, str):
                continue
            try:
                parsed = key_from_jwk(jwk)
            except JwtError, KeyError, ValueError, TypeError:
                # A directory is a third party's document. One unreadable entry
                # is their problem to fix and not a reason to refuse the rest.
                continue
            if isinstance(parsed, OkpPublicKey):
                keys[key_id] = parsed
        self._keys = keys
        return len(keys)


# The facts


@dataclass(frozen=True, slots=True)
class SignatureFacts:
    """What verification established about one request. Never a decision.

    Attributes:
        verified: A signature was present and verified against a known key.
        agent: The `Signature-Agent` the caller named, when it did.
        key_id: The `keyid` the signature named.
        reason: Why an unverified request was not verified. For logs and
            `doctor`, never for the client -- telling a caller which check
            failed hands them an oracle.
        covered: The component identifiers the signature covered, lowercased and
            in the order the caller listed them. **A policy that cares what was
            signed has to be able to ask**: `verified` alone says a signature
            checked out, not what it was a signature over, and the two answers
            differ by exactly the components a caller chose to leave out.
    """

    verified: bool = False
    agent: str | None = None
    key_id: str | None = None
    reason: str | None = None
    covered: tuple[str, ...] = ()


#: Shared result for the unsigned-request fast path.
_UNSIGNED: Final = SignatureFacts(reason="absent")


# Verification


class Signatures:
    """Verify inbound RFC 9421 signatures at ingress; publish the outcome.

    Registered as global middleware, so it covers route misses, static files and
    authorization failures alike -- which is the point, because the traffic this
    is for is trying to make the server pay for request construction before
    anything says no.

    Args:
        directories: Well-known Web Bot Auth directory URLs, https only.
        max_age: Seconds a `created` timestamp may differ from now, either way.
            Bounding the future too, so a forged forward timestamp cannot buy an
            arbitrarily long replay window.
        profile: The application profile to enforce. Only `WEB_BOT_AUTH_2026`.
        required: Components a signature must cover to be accepted.
        nonces: The replay ledger, or None to rely on `max_age` alone.
        refresh_on_startup: Fetch the directories during lifespan startup.
            Turn it off for a deployment that refreshes on its own schedule, or
            a test that installs keys directly -- it is the difference between
            `App(signatures=...)` reaching the network at boot and not.
        clock: A time source, for tests.

    Raises:
        ValueError: `max_age` is non-positive, `profile` is unknown, or a
            directory URL is not https.
    """

    global_scope = True
    __slots__ = (
        "_clock",
        "_directories",
        "_required",
        "max_age",
        "nonces",
        "profile",
        "refresh_on_startup",
        "refreshes",
        "refresh_errors",
        "unverified",
        "verified",
    )

    def __init__(
        self,
        *,
        directories: Iterable[str] = (),
        max_age: Any = 60.0,
        profile: str = WEB_BOT_AUTH_2026,
        required: Sequence[str] = _REQUIRED_COMPONENTS,
        nonces: NonceLedger | None = None,
        refresh_on_startup: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_age <= 0:
            raise ValueError("signature max_age must be positive")
        if profile != WEB_BOT_AUTH_2026:
            raise ValueError(
                f"unknown signature profile {profile!r}; "
                f"this build implements {WEB_BOT_AUTH_2026!r}"
            )
        self.profile = profile
        self.max_age = max_age
        self._required = tuple(name.lower() for name in required)
        self._directories = tuple(_Directory(url) for url in directories)
        self.nonces = nonces
        self.refresh_on_startup = refresh_on_startup
        self._clock = clock
        #: Requests whose signature verified.
        self.verified = 0
        #: Requests that carried a signature that did not verify. Distinct from
        #: unsigned requests, which are counted nowhere because they are normal.
        self.unverified = 0
        #: Directory refreshes attempted and failed. A refresh that has been
        #: failing is why a legitimate agent stopped verifying, and without the
        #: counter that looks like the agent's fault.
        self.refreshes = 0
        self.refresh_errors = 0

    @property
    def directories(self) -> tuple[str, ...]:
        """The configured directory URLs."""
        return tuple(directory.url for directory in self._directories)

    def _now(self) -> float:
        return time.time() if self._clock is None else self._clock()

    def install(self, url: str, document: Mapping[str, Any]) -> int:
        """Install a fetched directory document. Returns usable key count.

        Separate from fetching so the refresh transport is the deployment's
        choice and the tests need no network.

        Raises:
            KeyError: `url` is not a configured directory.
        """
        for directory in self._directories:
            if directory.url == url:
                return directory.install(document)
        raise KeyError(url)

    def facts(self, request: Any) -> SignatureFacts:
        """This request's verification outcome, resolved once per request.

        Cached on `request.state` the way `wreath._auth.cedar.request_flags` is,
        and for the same reason: several policies asking one question inside one
        decision must get one answer.
        """
        return resolve_once(request, _FACTS_SLOT, lambda: self._verify(request))

    def cedar_context(self, request: Any) -> dict[str, object]:
        """The facts, shaped for a Cedar `context`.

        Compose into an application's context provider:

        ```python
        def context(request):
            return {"method": request.method, **signatures.cedar_context(request)}

        authorizer = CedarAuthorizer(engine, context=context)
        ```

        `signature_verified` is **always present** and always a boolean, so both
        `when` and `unless` shapes read the same on an unsigned request;
        `signature_agent` is absent when there is none, so a policy testing it
        with `has` fails closed rather than matching an empty string.

        `signature_covered` is the covered component set, present whenever the
        signature verified. A policy that needs a request signed *end to end* --
        `context.signature_covered.contains("content-digest")` -- can say so,
        rather than reading `signature_verified` and hoping.
        """
        facts = self.facts(request)
        context: dict[str, object] = {"signature_verified": facts.verified}
        if facts.verified:
            context["signature_covered"] = list(facts.covered)
            if facts.agent is not None:
                context["signature_agent"] = facts.agent
        return context

    def before_sync(self, request: Any) -> None:
        """Resolve the facts for this request. Never short-circuits.

        Synchronous and non-blocking by construction: every input is already in
        memory, because key resolution never touches the network here.

        A request with no `Signature-Input` costs one lookup in the header index
        the request already built for CSRF and the auth backend, and returns.
        """
        headers = request._index_headers()
        if headers.get(b"signature-input") is None:
            request.state.__setattr__(_FACTS_SLOT, _UNSIGNED)
            return None
        self.facts(request)
        return None

    async def before(self, request: Any) -> None:
        """Compatibility wrapper; the compiled tape uses `before_sync`."""
        self.before_sync(request)
        return None

    def _verify(self, request: Any) -> SignatureFacts:
        headers = request._index_headers()
        raw_input = headers.get(b"signature-input")
        if raw_input is None:
            return _UNSIGNED
        raw_signature = headers.get(b"signature")
        if raw_signature is None:
            self.unverified += 1
            return SignatureFacts(reason="no-signature-header")
        agent_header = headers.get(b"signature-agent")
        agent = None if agent_header is None else agent_header.decode("latin-1").strip()
        if agent is not None and agent.startswith('"') and agent.endswith('"'):
            agent = agent[1:-1]
        try:
            facts = self._verify_headers(request, raw_input, raw_signature, agent, headers)
        except SignatureError as error:
            self.unverified += 1
            return SignatureFacts(agent=agent, reason=str(error))
        if facts.verified:
            self.verified += 1
        else:
            self.unverified += 1
        return facts

    def _verify_headers(
        self,
        request: Any,
        raw_input: bytes,
        raw_signature: bytes,
        agent: str | None,
        headers: Mapping[bytes, bytes],
    ) -> SignatureFacts:
        plan = _verification_plan(raw_input, raw_signature)
        params, raw_bytes, covered_order = _core.signature_plan_facts(plan)
        if not isinstance(raw_bytes, bytes):
            raise SignatureError("signature value must be a byte sequence")

        covered = set(covered_order)
        missing = [name for name in self._required if name not in covered]
        if missing:
            raise SignatureError(f"signature does not cover {missing[0]}")
        if _carries_body(headers) and _DIGEST_COMPONENT not in covered:
            # Not in `_required`, because a bodyless GET has nothing to digest
            # and demanding one would refuse every well-formed crawler request.
            # A request that *does* carry a body and covers no digest has signed
            # a message the body is not part of, which is the same hole as the
            # uncovered query one line up.
            raise SignatureError(f"signature does not cover {_DIGEST_COMPONENT}")
        # Parsed before the verify below, with the rest of the cheap refusals: a
        # malformed digest is a refusal that costs ~17us here and ~2.5ms there.
        expected_digest = (
            _digest_expectation(headers.get(b"content-digest"))
            if _DIGEST_COMPONENT in covered
            else None
        )

        created = params.get("created")
        if not isinstance(created, int):
            raise SignatureError("signature has no created parameter")
        now = self._now()
        if abs(now - created) > self.max_age:
            raise SignatureError("signature created outside the accepted window")
        expires = params.get("expires")
        # A malformed value must reach signature refusal, not an early TypeError.
        if isinstance(expires, int) and now > expires:
            raise SignatureError("signature has expired")

        alg = params.get("alg")
        if alg is not None and alg not in _ALGORITHMS:
            raise SignatureError(f"unsupported signature algorithm {alg!r}")

        key_id = params.get("keyid")
        if not isinstance(key_id, str):
            raise SignatureError("signature has no keyid")
        key = self._key(agent, key_id)
        if key is None:
            raise SignatureError("unknown signing key")

        nonce = params.get("nonce")
        nonces = self.nonces
        if nonces is not None:
            if not isinstance(nonce, str):
                raise SignatureError("signature has no nonce")
            ledger_key = f"{key_id}\x00{nonce}"
            # Check before verification, but spend bounded ledger capacity only after it.
            if nonces.seen(ledger_key):
                raise SignatureError("signature nonce was already used")

        # Curve verification follows every cheap refusal available to an untrusted caller.
        base = _core.signature_plan_base(_message(request, headers), plan, SignatureError, _derived)
        from ._auth._ecverify import verify_ed25519

        if not verify_ed25519(key.public, base, raw_bytes):
            raise SignatureError("signature does not verify")
        if nonces is not None:
            # The claim closes a race with the earlier read after verification succeeds.
            if not nonces.claim(ledger_key, now=None):
                raise SignatureError("signature nonce was already used")
        if expected_digest is not None:
            # **The body is not here yet.** This is global middleware and runs
            # before the first `receive`; draining the body here would break
            # streaming for every request in the application to check a header
            # almost none of them carry. So the expectation is parked and
            # `Request.body()`/`stream()` spend it -- the only places that can.
            # A mismatch raises there rather than downgrading the facts here,
            # because by the time a handler reads the body the authorization
            # decision has already been made on a signature that turns out not
            # to cover these bytes. There is no honest way to continue.
            request.state.__setattr__(_DIGEST_SLOT, expected_digest)
        return SignatureFacts(verified=True, agent=agent, key_id=key_id, covered=covered_order)

    def _key(self, agent: str | None, key_id: str) -> OkpPublicKey | None:
        """The named key from the agent's directory, or from any configured one.

        When the caller names a `Signature-Agent`, only that operator's
        directory is consulted -- otherwise operator A could verify with a key
        id that happens to collide with operator B's, and the `agent` fact would
        name the wrong party.
        """
        if agent is not None:
            for directory in self._directories:
                if directory.url == agent or directory.origin == agent:
                    return directory.key(key_id)
            return None
        for directory in self._directories:
            key = directory.key(key_id)
            if key is not None:
                return key
        return None

    async def refresh(self, client_factory: Any = None) -> int:
        """Fetch every configured directory. Returns keys installed.

        Off the request path by construction. Call it from lifespan startup --
        `App(signatures=...)` does -- and from a schedule if the deployment
        wants rotation picked up sooner than a restart.

        A directory that fails to refresh leaves its previous keys in place and
        increments `refresh_errors`: dropping them would turn a transient
        network fault into every agent silently becoming unverified.
        """
        from .http_client import HTTPClient

        factory = client_factory or (
            lambda origin: HTTPClient("wreath-signature-directory", base_url=origin)
        )
        installed = 0
        for directory in self._directories:
            self.refreshes += 1
            client = factory(directory.origin)
            try:
                document = await _fetch_directory(client, directory)
            except (OSError, ValueError, TypeError) as error:
                # Narrow on purpose: a transport failure or a malformed document
                # is survivable and counted; anything else is a programming
                # error and should not be absorbed here.
                self.refresh_errors += 1
                del error
                continue
            finally:
                close = getattr(client, "aclose", None)
                if close is not None:
                    await close()
            installed += directory.install(document)
        return installed


async def _fetch_directory(client: Any, directory: _Directory) -> Mapping[str, Any]:
    import json

    response = await client.get(directory.path)
    body = getattr(response, "content", None)
    if body is None:
        body = await response.read()
    if len(body) > 512 * 1024:
        raise ValueError("signature directory document is too large")
    document = json.loads(body)
    if not isinstance(document, Mapping):
        raise ValueError("signature directory is not a JSON object")
    return document


def _message(request: Any, headers: Mapping[bytes, bytes]) -> RequestMessage:
    host = headers.get(b"host")
    return RequestMessage(
        method=request.method,
        scheme=request.scheme,
        authority="" if host is None else host.decode("latin-1"),
        path=request.path,
        query=request.query_string,
        headers=headers,
    )


# Signing -- the same base, the other way round


def sign_request(
    key: SigningKey,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    components: Sequence[str] = _REQUIRED_COMPONENTS,
    created: int | None = None,
    expires_in: int | None = None,
    nonce: str | None = None,
    label: str = "sig1",
    tag: str | None = None,
) -> dict[str, str]:
    """Sign an outbound request. Returns the headers to add.

    The mirror of verification, over the *same* `signature_base`, which is what
    makes the pair testable against each other and against the RFC's vectors.

    ```python
    headers = sign_request(key, method="GET", url="https://api.example.com/v1/x")
    await client.get("/v1/x", headers=headers)
    ```

    **Pass `body` when there is one.** It is hashed into a `Content-Digest`
    header, which is added to both the returned headers and the covered set, and
    that is the only thing that makes the signature a signature over the payload
    rather than over the endpoint. A verifier configured with this module's
    default required set refuses a bodied request that omits it.

    Args:
        body: The request body, hashed into `Content-Digest` and covered.
        components: What to cover. Defaults to the required set; `content-digest`
            is appended when `body` is given and is not already listed.

    Raises:
        SignatureError: A component cannot be built from the given request.
        ValueError: `url` is not absolute.
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ValueError("sign_request needs an absolute url")
    authority, _, remainder = rest.partition("/")
    path, _, query = ("/" + remainder).partition("?")
    raw = {
        name.lower().encode("ascii"): value.encode("latin-1")
        for name, value in (headers or {}).items()
    }
    raw.setdefault(b"host", authority.encode("latin-1"))
    covered = tuple((name.lower(), {}) for name in components)
    digest_header: str | None = None
    if body is not None:
        digest_header = (
            "sha-256=:" + base64.b64encode(_digest("sha-256", body)).decode("ascii") + ":"
        )
        raw[b"content-digest"] = digest_header.encode("ascii")
        if all(name != _DIGEST_COMPONENT for name, _params in covered):
            covered = (*covered, (_DIGEST_COMPONENT, {}))
    message = RequestMessage(
        method=method,
        scheme=scheme,
        authority=authority,
        path=path,
        query=query.encode("latin-1"),
        headers=raw,
    )
    stamp = int(time.time()) if created is None else created
    params: dict[str, Any] = {"created": stamp, "keyid": key.key_id, "alg": "ed25519"}
    if expires_in is not None:
        params["expires"] = stamp + expires_in
    if nonce is not None:
        params["nonce"] = nonce
    if tag is not None:
        params["tag"] = tag
    base = signature_base(message, covered, params)
    signature = key.sign(base)
    if len(signature) != 64:
        raise SignatureError("an ed25519 signature must be 64 bytes")
    serialized = " ".join(_serialize_component(name, {}) for name, _ in covered)
    out = {
        "Signature-Input": f"{label}=({serialized}){_serialize_params(params)}",
        "Signature": f"{label}=:{base64.b64encode(signature).decode('ascii')}:",
    }
    if digest_header is not None:
        out["Content-Digest"] = digest_header
    if key.agent is not None:
        out["Signature-Agent"] = _serialize_string(key.agent)
    return out


# Crawler policy, derived from the route table


@dataclass(frozen=True, slots=True)
class CrawlerPolicy:
    """What a crawler is told, derived from the routes that exist.

    Attributes:
        allow: Paths a caller may reach with no identity at all.
        disallow: Paths that ask something of the caller.
    """

    allow: tuple[str, ...]
    disallow: tuple[str, ...]


def crawler_policy(app: Any) -> CrawlerPolicy:
    """Split an application's routes into public and not.

    "Public" is `AuthRequirement.access_level == 0` -- the framework's own
    single definition of "this endpoint asks nothing of the caller", already
    read by both dispatchers, by `Wreath._authorize_request` and by MCP. Using
    it here rather than a second predicate is the whole point: a hand-maintained
    `robots.txt` drifts from the route table on the first refactor, and this one
    cannot.

    A path with parameters is reduced to its static prefix, because `robots.txt`
    has no notion of `{id}` and a literal `/photos/{id}` matches nothing.
    """
    allow: set[str] = set()
    disallow: set[str] = set()
    for route, requirement in zip(_routes(app), app._application_image.requirements(), strict=True):
        if not getattr(route, "include_in_schema", True):
            continue
        target = _crawlable_path(route.path)
        if requirement.access_level == 0:
            allow.add(target)
        else:
            disallow.add(target)
    # A prefix that has any protected route under it is not advertised as open:
    # the narrower statement is the honest one.
    allow -= disallow
    return CrawlerPolicy(allow=tuple(sorted(allow)), disallow=tuple(sorted(disallow)))


def _routes(app: Any) -> tuple[Any, ...]:
    """This application's compiled route definitions.

    Through `_application_image`, which is what `wreath.openapi` already asks --
    one accessor for "enumerate the routes", so a change to where they live has
    one call site to follow rather than two.
    """
    return app._application_image.routes()


def _crawlable_path(path: str) -> str:
    head, brace, _ = path.partition("{")
    if not brace:
        return path
    if head.endswith("/"):
        # "/articles/{slug}" -> "/articles/", and "/{slug}" -> "/", never "//".
        return head if head != "/" else "/"
    return head.rstrip("/") or "/"


def robots_txt(app: Any, *, sitemap: str | None = None, crawl_delay: int | None = None) -> str:
    """A `robots.txt` body derived from `app`'s routes.

    An honour-system control, and worth saying so where somebody will read it:
    a crawler that ignores this is not stopped by it. The enforced half is a
    signature check and a policy. This is for the crawlers that do behave, and
    its value is that it cannot drift from what the application actually serves.
    """
    http_policy = getattr(app, "_http_policy", None)
    ai_scraping = getattr(http_policy, "ai_scraping", None)
    blocked_products = getattr(ai_scraping, "blocked_products", ())
    lines = [f"User-agent: {product}" for product in blocked_products]
    if blocked_products:
        lines += ["Disallow: /", ""]
    lines += ["User-agent: *"]
    lines += [f"Disallow: {path}" for path in robots_disallow(app)]
    lines += [f"Allow: {path}" for path in crawler_policy(app).allow]
    if crawl_delay is not None:
        lines.append(f"Crawl-delay: {crawl_delay}")
    if sitemap is not None:
        lines.append(f"Sitemap: {sitemap}")
    return "\n".join(lines) + "\n"


def robots_disallow(app: Any) -> tuple[str, ...]:
    """The protected paths, deduplicated to their shortest covering prefixes."""
    return _core.minimal_prefixes(crawler_policy(app).disallow)


def llms_txt(app: Any, *, title: str, summary: str | None = None) -> str:
    """An `llms.txt` body naming what this application offers a model.

    The same derivation as `robots_txt` and the same honesty: a declaration, not
    an enforcement. Public routes carrying a summary become the listing, because
    a route with no description is not something a model can use.
    """
    lines = [f"# {title}", ""]
    if summary is not None:
        lines += [f"> {summary}", ""]
    lines.append("## Endpoints")
    lines.append("")
    requirements = {
        id(route): requirement
        for route, requirement in zip(
            _routes(app), app._application_image.requirements(), strict=True
        )
    }
    for route in sorted(_routes(app), key=lambda item: item.path):
        if requirements[id(route)].access_level != 0:
            continue
        if not getattr(route, "include_in_schema", True):
            continue
        description = route.summary or (route.endpoint.__doc__ or "").strip().split("\n")[0]
        if not description:
            continue
        methods = "/".join(sorted(route.methods))
        lines.append(f"- [{methods} {route.path}]({route.path}): {description}")
    return "\n".join(lines) + "\n"


# 402


class PaymentRequired(HTTPException):
    """Answer an agent with a price rather than a wall.

    A response *shape*, deliberately not a payment integration. Pay-Per-Crawl
    normalised answering an unpaid agent with a 402 and terms; four competing
    agent-payment protocols were still live as of mid-2026 (x402 under a Linux
    Foundation body from July 2026, Google's AP2, Stripe's MPP, ACP), and
    blessing one in a framework would age badly within a year. `scheme` is the
    application's choice and settlement is entirely its business.

    ```python
    if not paid(request):
        raise PaymentRequired(
            amount="0.002", currency="USD", pay_to="https://pay.example/x"
        )
    ```

    An `HTTPException`, so it travels the ordinary error boundary, renders as an
    RFC 9457 problem document like every other refusal, and gets its challenge
    header copied on by the same code that copies a 401's -- rather than a
    second response path that would have to learn all of that again.
    """

    status = 402

    __slots__ = ("amount", "currency", "pay_to", "scheme")

    def __init__(
        self,
        *,
        amount: str,
        currency: str,
        pay_to: str,
        scheme: str = "http-402",
        detail: str = "This resource requires payment.",
    ) -> None:
        self.amount = amount
        self.currency = currency
        self.pay_to = pay_to
        self.scheme = scheme
        super().__init__(detail, headers=((b"accept-payment", self.terms()),))

    def terms(self) -> bytes:
        """The `Accept-Payment` challenge, as a structured-field item."""
        return (
            f'{self.scheme};amount="{self.amount}"'
            f';currency="{self.currency}";pay-to="{self.pay_to}"'
        ).encode("ascii")
