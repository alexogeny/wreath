"""Source-level security rules — the tier that reads the application's own code.

The other two tiers look at what an application *emits*: rendered HTML, live
response headers. Neither can see the defect classes that do the real damage,
because those leave no trace in a correct-looking 200. A query built by string
formatting, a signing key that is a literal, a comparison that returns early on
the first wrong byte -- all of them serve a perfectly ordinary response.

So this tier parses the source instead. It is a **curated** ruleset, not a
general-purpose linter. A rule earns its place by meeting two tests: the defect
class ships in real applications rather than in exercises, and it has a safe
spelling that Wreath already provides. That second half is what makes the
suggestions worth reading -- a finding does not say "this is dangerous", it
says "you wrote X; the primitive for this is Y".

## Three questions, not one

Most of the rules ask *is this expression dangerous?* -- of a call, or of a
comparison. Two further questions need the same walk and no more machinery, and
between them they cover the defects that expression-level rules structurally
cannot see:

* **What does this declaration say?** A signing key is most often the default
  value of a settings field, not an argument to a call. Nothing complains at
  startup, because from the application's point of view the setting is
  populated -- with the key that is in the source.
* **What happens on the branch where the check could not be made?** A control
  that raises on denial and *returns* on "cannot tell" has two exits and one of
  them is open. The defect is never the `return`; it is that the undecidable
  case and the permitted case are spelled identically, so no caller can tell
  them apart.

## Precision over recall, deliberately

A security linter that cries wolf gets suppressed wholesale, and then it is
worse than nothing: the suppression outlives the person who understood it.
So each rule is written to be quiet on the correct form of the same intent, and
`tests/audit/test_code_rules.py` asserts both halves -- the vulnerable shape
fires, the safe shape does not. A rule that cannot be made quiet does not ship.

Two consequences worth stating:

* **There is no general path-traversal rule.** Catching `Path(root) / name`
  requires knowing that `name` came from a route parameter, and route
  parameters are only knowable from the application object rather than from one
  file. The archive-member case *is* covered, because there the provenance is
  visible in the same expression. General traversal waits for the app-level
  tier.
* **Taint starts at the route boundary, and that is what makes it precise.**
  A handler is identifiable from its decorator alone -- `@router.get(...)`,
  `@app.post(...)` -- so its parameters are known to be caller-controlled
  without needing the application object. Everything else is inferred one hop
  at a time from there.

  This distinction is the whole rule set. Interpolating a *module constant*
  into SQL is how you write a schema-qualified statement, and Wreath's own
  `_locks.py` does it in seven places; interpolating a *handler parameter* is
  an injection. A first draft of this file did not separate them and reported
  103 findings against Wreath's own source and six against a correct example
  application -- which is precisely the "cries wolf, gets suppressed wholesale"
  failure this module claims to avoid.

## Adding a rule

A rule is a row in `CODE_RULES` plus the detection in `_Scanner`, and a pair of
tests: one shape it catches, one shape it must not. The row carries the CWE and
the Wreath primitive that replaces the defect, because a finding with no
remediation is a complaint.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from ...crud import SENSITIVE_FIELD
from ..model import Finding, Severity

__all__ = ["CODE_RULES", "CodeRule", "scan_source"]


@dataclass(frozen=True)
class CodeRule:
    """One rule's identity and remediation. Detection lives in `_Scanner`."""

    rule_id: str
    severity: Severity
    #: CWE identifier, so a finding maps onto an external taxonomy.
    reference: str
    #: The Wreath primitive (or stdlib call) that replaces the defect.
    suggestion: str
    #: One line, for the reference table in the docs.
    summary: str


CODE_RULES: tuple[CodeRule, ...] = (
    CodeRule(
        "sql-interpolation",
        Severity.ERROR,
        "CWE-89",
        "pass values as $1, $2 parameters to Session.raw; Wreath never rewrites raw SQL",
        "SQL built by string interpolation reaches the database unmodified",
    ),
    CodeRule(
        "timing-unsafe-compare",
        Severity.ERROR,
        "CWE-208",
        "compare secrets with hmac.compare_digest, which does not return early",
        "a secret compared with == leaks its prefix through response timing",
    ),
    CodeRule(
        "weak-randomness",
        Severity.ERROR,
        "CWE-338",
        "use secrets.token_urlsafe / secrets.choice for anything an attacker must not guess",
        "a security value drawn from random, which is a predictable Mersenne Twister",
    ),
    CodeRule(
        "hardcoded-secret",
        Severity.ERROR,
        "CWE-798",
        "read the key from the environment or a secret store; never a literal in source",
        "a signing key or password written as a string literal",
    ),
    CodeRule(
        "ssrf-policy-widened",
        Severity.ERROR,
        "CWE-918",
        "leave DestinationPolicy at its defaults and name the hosts you mean with hosts=",
        "an outbound client permitted to reach private, loopback or link-local addresses",
    ),
    CodeRule(
        "unsafe-xml-parser",
        Severity.ERROR,
        "CWE-611",
        "parse with wreath.xml.parse, which refuses DOCTYPE and every non-predefined entity",
        "XML read with a parser that resolves external entities",
    ),
    CodeRule(
        "template-from-request",
        Severity.ERROR,
        "CWE-1336",
        "compile templates from a TemplateDirectory at startup, never from a request",
        "a template compiled from a value that is not a literal",
    ),
    CodeRule(
        "dynamic-import",
        Severity.ERROR,
        "CWE-470",
        "map a closed vocabulary of names to functions; never resolve a caller's string",
        "a module, attribute or expression resolved from data",
    ),
    CodeRule(
        "unsafe-archive-extract",
        Severity.ERROR,
        "CWE-22",
        "extract with wreath.objects.unzip_stream and ZipExtractionLimits",
        "an archive extracted without member, size, ratio or symlink limits",
    ),
    CodeRule(
        "path-from-request",
        Severity.ERROR,
        "CWE-22",
        "normalise the name with wreath.objects.normalize_key and read it through a "
        "wreath.storage backend, which opens beneath a root descriptor",
        "a filesystem path joined from a value the caller chose",
    ),
    CodeRule(
        "mass-assignment",
        Severity.ERROR,
        "CWE-915",
        "declare the body as a dataclass or ORM model; Wreath rejects unknown fields",
        "a request body walked onto an object with setattr",
    ),
    CodeRule(
        "case-mapped-authz",
        Severity.WARN,
        "CWE-178",
        "compare the stored, normalised value; upper()/lower()/casefold() are not injective",
        "an authorization decision made after a Unicode case mapping",
    ),
    CodeRule(
        "cors-reflect-origin",
        Severity.ERROR,
        "CWE-942",
        "use CorsPolicy with an explicit allow_origins list",
        "the request Origin reflected into Access-Control-Allow-Origin",
    ),
    CodeRule(
        "debug-enabled",
        Severity.WARN,
        "CWE-489",
        "drive debug from configuration so production cannot inherit a developer's value",
        "the application constructed with debug=True as a literal",
    ),
    CodeRule(
        "untrusted-forwarded-header",
        Severity.WARN,
        "CWE-348",
        "establish the header with ProxyPolicy(trusted=...) before reading it",
        "a forwarded client address read without a configured proxy trust boundary",
    ),
    CodeRule(
        "wildcard-trust-list",
        Severity.ERROR,
        "CWE-346",
        "name the hosts, origins or CIDRs you mean; a trust list of '*' is not a boundary",
        "a trust boundary configured to accept every peer",
    ),
    CodeRule(
        "secret-in-log",
        Severity.ERROR,
        "CWE-532",
        "hold it in wreath.config.Secret, whose repr and str are redacted, and log through "
        "wreath.logging, which redacts by default",
        "a credential or a caller's own body formatted into a log record",
    ),
    CodeRule(
        "authz-fail-open",
        Severity.ERROR,
        "CWE-863",
        "raise on the undecidable branch too; an authorizer answers Allow or Deny and has no "
        "third answer",
        "an authorization check that returns, rather than refuses, when it cannot decide",
    ),
    CodeRule(
        "auth-disable-flag",
        Severity.ERROR,
        "CWE-1188",
        "give a test a test principal; there is no supported way to switch authentication off",
        "a configuration flag that skips authentication entirely",
    ),
    CodeRule(
        "auth-fallback-on-exception",
        Severity.ERROR,
        "CWE-1390",
        "verify with one JwtVerifier and one key source; catch the specific error and refuse",
        "an authentication path that retries with a weaker verifier when the strong one raises",
    ),
    CodeRule(
        "outbound-url-from-request",
        Severity.ERROR,
        "CWE-918",
        "give the client a DestinationPolicy naming the hosts you mean; it checks every DNS "
        "answer and every redirect, not just the string you were handed",
        "an outbound request whose destination the caller chose",
    ),
    CodeRule(
        "substring-security-match",
        Severity.ERROR,
        "CWE-697",
        "compare whole values: str.startswith for a prefix, a frozenset for membership, a Cedar "
        "action for a policy",
        "a security decision made by substring, so a longer value satisfies a shorter rule",
    ),
    CodeRule(
        "error-detail-leaked",
        Severity.WARN,
        "CWE-209",
        "write the refusal the caller should read; the Flight Recorder keeps the diagnosis",
        "a caught exception's own text returned to the caller",
    ),
    CodeRule(
        "env-conditional-security",
        Severity.WARN,
        "CWE-1188",
        "declare it in wreath.config with a secure default, so weakening it is a value someone "
        "can see rather than a branch in the source",
        "a security control whose strength depends on which environment is running",
    ),
    CodeRule(
        "unparseable",
        Severity.WARN,
        "wreath:audit",
        "check the file parses under the interpreter this audit runs on",
        "the file could not be parsed, so no rule could be applied to it",
    ),
)

_BY_ID = {rule.rule_id: rule for rule in CODE_RULES}

#: Ruff's `flake8-bandit` codes that mean the same thing as one of these rules.
#:
#: **A finding the project has already declared and justified is not reported
#: again.** Wreath's own ORM compiler carries a reviewed `S102` waiver on `exec`
#: calls with a written reason, and re-raising those under a second name is how
#: the second tool gets switched off. `AGENTS.md` asks for suppressions to be
#: declared, scoped and reasoned; honouring the declaration is the other half
#: of that bargain.
_EQUIVALENT_NOQA = {
    "dynamic-import": ("S102", "S307"),
    "sql-interpolation": ("S608",),
    "hardcoded-secret": ("S105", "S106", "S107"),
    "weak-randomness": ("S311",),
    "unsafe-xml-parser": ("S314", "S405", "S320"),
    "unsafe-archive-extract": ("S202",),
}

#: The audit's own marker, for a finding ruff has no code for:
#:
#:     # wreath-audit: allow case-mapped-authz -- the list is ASCII by construction
#:
#: A bare marker with no reason is itself a finding, exactly as for the native
#: lints -- so the reason is required and is echoed in `--json`.
_WAIVER = "wreath-audit: allow"


# Kept as data rather than inline so the docs table and the tests can quote
# them, and so widening one is a reviewable one-line change.

#: Names that are a secret whatever else is in the expression.
_SECRET_WORDS = (
    "secret",
    "hmac",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "otp",
    "totp",
    "salt",
    "credential",
)

#: Names that are *sometimes* a secret. A route signature, a lexer token, a
#: plan digest and a protocol nonce all live in Wreath under these words, and
#: flagging them accounted for six false positives. They fire only opposite a
#: name that gives the comparison an authentication role.
_WEAK_SECRET_WORDS = ("token", "signature", "digest", "mac")

#: The second signal: one side names what the other is being checked against.
_COMPARISON_ROLES = (
    "expected",
    "provided",
    "given",
    "supplied",
    "received",
    "candidate",
    "computed",
    "presented",
    "claimed",
)

#: Suffixes that make a name an identifier or a tag rather than a secret.
#: `credential_id` is a primary key; `_TOKEN_VERSION` is a format marker;
#: `token_uid` is what a resource is called, not what authorises reaching it.
#: `SESSION_SECRET_VARIABLE = "APP_SESSION_SECRET"` is the *name of the
#: environment variable* the secret is read from, which is the opposite of a
#: secret in the source -- it is the mechanism for keeping one out of it.
_NOT_SECRET_SUFFIXES = (
    "_id",
    "_ids",
    "_name",
    "_version",
    "_type",
    "_kind",
    "_key_id",
    "_uid",
    "_uuid",
    "_variable",
    "_var",
    "_env",
    "_envvar",
    "_setting",
    "_field",
    "_header",
    "_param",
)
#: Deliberately excludes a bare "key" (`for key, value in ...` is everywhere),
#: and "auth"/"sig", which matched `oauth2` helpers and anything with "signal"
#: in the name across 25 false positives in Wreath's own source.
_SECRET_EXACT = ("pin",)

#: Keyword arguments whose literal value is a credential.
_SECRET_KEYWORDS = frozenset(
    {
        "secret",
        "secret_key",
        "url_secret",
        "password",
        "api_key",
        "apikey",
        "token",
        "private_key",
        "signing_key",
    }
)

#: Constructors whose *first positional* argument is a signing key.
_POSITIONAL_SECRET_CALLEES = frozenset({"SessionPolicy", "CsrfPolicy"})

#: Modules that will resolve an external entity if asked.
_UNSAFE_XML_MODULES = ("xml.sax", "xml.dom", "xml.etree", "xml.parsers", "lxml")

#: Statement-level sinks that execute SQL exactly as given.
_SQL_SINKS = frozenset({"raw", "declared", "execute", "fetch", "fetchrow", "fetchval"})

#: Keyword arguments that configure a trust boundary. A literal `"*"` in one of
#: these is unambiguous: it is the boundary switched off.
_TRUST_KEYWORDS = frozenset(
    {"trusted", "trusted_hosts", "trusted_proxies", "trusted_origins", "allowed_hosts"}
)

#: Origins are the exception, and are handled separately: a public read-only API
#: is entitled to answer any origin, so `allow_origins=["*"]` alone is a design
#: decision rather than a defect. It becomes one opposite credentials, which is
#: the single pair `CorsPolicy` refuses at construction.
_ORIGIN_KEYWORDS = frozenset({"allow_origins", "allow_origin", "origins"})

#: Headers that carry a client address only when a proxy is trusted.
_FORWARDED_HEADERS = frozenset(
    {"x-forwarded-for", "x-real-ip", "x-client-ip", "forwarded", "true-client-ip"}
)

#: Names that identify a principal. The case-mapping rule needs one of these
#: on the mapped side: a CORS preflight uppercases an HTTP *method*, and a
#: method is not a principal.
_IDENTITY_WORDS = (
    "email",
    "user",
    "login",
    "principal",
    "account",
    "name",
    "subject",
    "actor",
    "identity",
    "address",
)

#: Containers whose membership decides authorization.
_AUTHZ_WORDS = (
    "allow",
    "permit",
    "admin",
    "ops",
    "staff",
    "role",
    "whitelist",
    "allowlist",
    "member",
    "privileged",
    "superuser",
)

_CASE_MAPPINGS = frozenset({"upper", "lower", "casefold", "title", "swapcase"})

#: Decorator attributes that mark a function as reachable from the network.
_ROUTE_DECORATORS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "route", "websocket"}
)

#: Handler parameters that are framework-injected rather than caller-supplied.
_INJECTED_PARAMS = frozenset({"self", "cls", "request", "websocket"})

#: Annotations that mark a parameter as framework-injected.
_INJECTED_ANNOTATIONS = ("Session", "FromORM", "Depends", "Request", "AppScope")

#: Attributes that write a log record. The receiver is checked too, so
#: `results.info` is not mistaken for a logger.
_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)

#: Names that hold a directory the application reads or writes under. The left
#: half of `path-from-request`: `EXPORT_ROOT / name` is a join, `total / count`
#: is division, and only the name says which.
_PATH_ROOT_WORDS = (
    "root",
    "dir",
    "directory",
    "path",
    "base",
    "folder",
    "store",
    "home",
    "media",
    "uploads",
    "exports",
    "storage",
)

#: The provenance half of `timing-unsafe-compare`. Two secret-named operands
#: whose provenance differs are a presented value being checked against a held
#: one -- the same signal `_COMPARISON_ROLES` carries, in the spelling a
#: double-submit check actually uses.
_PROVENANCE_WORDS = (
    "cookie",
    "header",
    "session",
    "stored",
    "saved",
    "request",
    "client",
    "server",
    "incoming",
    "submitted",
    "body",
    "query",
    "param",
    "form",
)

#: Names that mean "authentication is off". Deliberately the disabling half
#: only: a flag that turns a control *on* is configuration, and one that turns
#: it off is a backdoor with a config key attached. Nobody writes these by
#: accident, which is what lets the list stay short and the rule stay quiet.
_AUTH_DISABLE_WORDS = (
    "no_auth",
    "noauth",
    "auth_disabled",
    "disable_auth",
    "disable_authentication",
    "skip_auth",
    "skip_authentication",
    "bypass_auth",
    "auth_bypass",
    "allow_anonymous",
    "anonymous_ok",
    "insecure_skip_verify",
    "auth_off",
    "disable_security",
)

#: Values whose weak setting is a vulnerability rather than a preference. The
#: `env-conditional-security` rule is narrow on purpose: environments differ,
#: and that is what they are for.
_SECURITY_FLAG_NAMES = frozenset(
    {
        "secure",
        "httponly",
        "http_only",
        "samesite",
        "same_site",
        "csrf",
        "csrf_enabled",
        "csrf_required",
        "verify",
        "verify_ssl",
        "ssl_verify",
        "tls_verify",
        "check_hostname",
        "hsts",
        "strict_transport_security",
        "signature_required",
        "auth_required",
        "authentication_required",
        "authorization_required",
    }
)

#: Names that mean "which deployment is this?". The security-flag rule needs one
#: of these on the deciding side, so an ordinary feature toggle stays quiet.
_ENVIRONMENT_WORDS = (
    "env",
    "environment",
    "stage",
    "deployment",
    "debug",
    "devel",
    "development",
    "testing",
    "local",
)

#: Exceptions that refuse a request on authorization grounds.
_AUTHZ_EXCEPTIONS = (
    "forbidden",
    "unauthoris",
    "unauthoriz",
    "authoris",
    "authoriz",
    "permission",
    "denied",
    "notpermitted",
    "accessdenied",
)

#: Status codes that make an `HTTPException(...)`-shaped call a refusal.
_REFUSAL_STATUSES = frozenset({401, 403})

#: Names that mark a function as deciding who the caller is. `auth-fallback-on-
#: exception` is only about these; `AGENTS.md` legislates broad handlers
#: generally, and a rule that repeated it everywhere would be a lint, not a
#: security finding.
_AUTHENTICATION_FUNCTIONS = (
    "authenticate",
    "authentication",
    "verify",
    "validate_token",
    "login",
    "identify",
    "decode_token",
    "check_token",
    "current_user",
    "principal",
)

#: Calls that verify a credential. The handler of a broad `except` reaching for
#: one of these is the fallback this rule is named for.
_VERIFIER_CALLS = ("decode", "verify", "authenticate", "validate", "unseal", "check_token")

#: Callables whose argument becomes the response body. A caught exception's own
#: text reaching one of these is free reconnaissance for the caller.
_RESPONSE_CALLEES = (
    "httpexception",
    "badrequest",
    "unauthorized",
    "forbidden",
    "notfound",
    "conflict",
    "unprocessableentity",
    "internalerror",
    "internalservererror",
    "toomanyrequests",
    "payloadtoolarge",
    "response",
    "jsonresponse",
    "plaintextresponse",
    "htmlresponse",
    "problemresponse",
    "abort",
)

#: Keyword arguments that carry response text.
_RESPONSE_TEXT_KEYWORDS = frozenset({"detail", "content", "message", "body", "text", "reason"})

#: Receivers that make `.get`/`.post` an outbound request rather than a mapping
#: lookup. `.get` is `dict.get` far more often than it is an HTTP verb, and the
#: caller's own body is the most common receiver of both -- so the rule decides
#: on the receiver, not on the verb.
#: `session` is deliberately absent: an ORM session is far more common in a
#: Wreath application than a `requests.Session`, and `self._session.delete(row)`
#: is not an outbound request.
_HTTP_CLIENT_NAMES = ("client", "http", "requests", "httpx", "urllib", "aiohttp", "fetch")

#: Methods that issue an outbound request.
_HTTP_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send"}
)

#: Left-hand operands whose membership test is a security decision. A substring
#: match against one of these is the finding; `character in "aeiou"` is not.
_MATCH_CONTEXT_WORDS = (
    "header",
    "method",
    "scheme",
    "path",
    "route",
    "url",
    "role",
    "scope",
    "permission",
    "action",
    "origin",
    "host",
    "claim",
    "audience",
)

#: Right-hand operands that are a policy, a path or a scope *string*. Paired
#: with a `str` annotation, this is what separates `"admin" in roles` -- correct
#: code over a collection -- from `"admin" in scope_string`, which is not.
_MATCH_SUBJECT_WORDS = (
    "path",
    "url",
    "route",
    "endpoint",
    "condition",
    "scope",
    "scopes",
    "permission",
    "permissions",
    "policy",
    "authorities",
    "claims",
    "target",
)

#: Below this, a string literal on the right of `in` is a character class
#: (`c in "aeiou"`) rather than a value somebody meant to compare.
_MEMBERSHIP_LITERAL_LENGTH = 6


def _wildcard_in(node: ast.AST) -> bool:
    """Whether this value is, or contains, a literal `"*"`."""
    if isinstance(node, ast.Constant):
        return node.value == "*"
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_wildcard_in(element) for element in node.elts)
    return False


def _establishes_trust(node: ast.Call) -> bool:
    """Whether a `ProxyPolicy(...)` call names a real boundary."""
    for keyword in node.keywords:
        if keyword.arg == "trusted":
            return not _wildcard_in(keyword.value)
    return False


def _is_route_decorator(node: ast.AST) -> bool:
    """Whether this decorator publishes the function on the network."""
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS


def _mentions(name: str, words: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in words)


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(_NOT_SECRET_SUFFIXES):
        return False
    if _mentions(lowered, _SECRET_WORDS):
        return True
    # Exact-match vocabulary: short words that would over-match as substrings.
    # "pin" inside "pinned".
    return any(part in _SECRET_EXACT for part in lowered.replace("-", "_").split("_"))


def _is_weak_secret_name(name: str) -> bool:
    lowered = name.lower()
    return not lowered.endswith(_NOT_SECRET_SUFFIXES) and _mentions(lowered, _WEAK_SECRET_WORDS)


def _is_credential_name(name: str) -> bool:
    """Whether this name holds a credential.

    `crud.SENSITIVE_FIELD` is the repository's one spelling of this vocabulary
    -- it already carries `token`, `authorization`, `cookie`, `credential` and
    the `*_key` family, with its exclusions written down -- so this reuses it
    rather than growing a fourth list beside `_SECRET_WORDS`. The identifier
    suffixes still apply on top: `token_uid` names a resource, not the thing
    that authorises reaching it.
    """
    lowered = name.lower()
    if lowered.endswith(_NOT_SECRET_SUFFIXES):
        return False
    return _is_secret_name(lowered) or bool(SENSITIVE_FIELD.search(lowered))


#: How long a declared literal must be before it is a key rather than a format
#: marker. `"HS256"`, `"Bearer"` and a header name all sit under it; a signing
#: key does not, because nothing usable is shorter than this.
_DECLARED_SECRET_LENGTH = 16

#: The characters a key is drawn from -- hex, base64, base64url. Anything else
#: in the literal means it is a sentinel or a URN rather than a secret.
_KEY_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/=-_")


def _looks_like_a_key(value: str | bytes) -> bool:
    """Whether a declared literal is a key rather than a name that reads like one.

    Length alone is not enough, and the four literals that proved it all share
    one shape -- they are *identifiers*, not keys: a sentinel standing for "no
    password", two `request.state` keys, and a SAML URN. Each is long, each sits
    under a name from the credential vocabulary, and none is a secret.

    What separates them is the alphabet and the digits. A key is hex or base64
    and carries both letters and digits; a readable identifier carries a colon,
    a bang, a space, or no digit at all. The deliberate cost is a declared
    *passphrase* with no digit in it, which this will not see -- the keyword
    rule still catches one that is passed to anything, and precision here is
    worth more than that case.
    """
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    if len(text) < _DECLARED_SECRET_LENGTH or not set(text) <= _KEY_ALPHABET:
        return False
    if _looks_like_a_development_key(text):
        return True
    return any(character.isdigit() for character in text) and any(
        character.isalpha() for character in text
    )


#: Words that make a readable literal a *development* key rather than an
#: identifier. Closing the gap the docstring above declares: a passphrase with
#: no digit in it, which the alphabet test cannot see.
_DEVELOPMENT_KEY_WORDS = (
    "secret",
    "password",
    "passphrase",
    "changeme",
    "change-me",
    "change_me",
    "insecure",
    "placeholder",
    "dev-",
    "-dev",
    "_dev",
    "dev_",
    "test-",
    "-test",
    "local-",
    "-local",
    "example",
    "sample",
    "dummy",
    "notsecure",
    "topsecret",
    "hunter2",
    "letmein",
    "unsafe",
    "donotuse",
    "do-not-use",
)


def _looks_like_a_development_key(text: str) -> bool:
    """Whether a readable literal is a key somebody wrote to get going.

    The alphabet test above separates keys from identifiers by looking for the
    shape of generated material -- hex, base64, letters *and* digits. It has one
    declared blind spot, and the blind spot is the single most common way a key
    actually reaches production: somebody types `"northwind-dev-secret"` to make
    the application start on their laptop, and it ships, because there is
    nothing about it for a review to catch. It has no digits, so it is not
    generated material; it is under a credential name, so it is not an
    identifier either.

    So: a credential-named literal that says in words what it is gets flagged on
    the strength of those words. The vocabulary is deliberately the vocabulary
    of *stand-ins* -- a real key does not contain "changeme" -- which is what
    keeps this off the URNs, state keys and algorithm names the alphabet test
    was written to exclude.
    """
    lowered = text.lower()
    return any(word in lowered for word in _DEVELOPMENT_KEY_WORDS)


def _is_security_flag(name: str) -> bool:
    """Whether this name holds a value whose weak setting is a vulnerability."""
    lowered = name.lower()
    return (
        lowered in _SECURITY_FLAG_NAMES
        or lowered.startswith(("require_", "enforce_"))
        or lowered.endswith("_required")
    )


def _is_logger_name(part: str) -> bool:
    """Whether this receiver is a logger, and not merely a `catalog`."""
    stripped = part.lower().strip("_")
    return stripped in ("log", "logger", "logging") or stripped.startswith(
        ("log_", "logger", "logging")
    )


def _provenance(name: str) -> frozenset[str]:
    """Where a value came from, as far as its name admits."""
    lowered = name.lower()
    return frozenset(word for word in _PROVENANCE_WORDS if word in lowered)


def _expression_names(node: ast.AST) -> list[str]:
    """Every name and attribute mentioned in an expression.

    Both halves matter: `credentials.as_dict()` is a secret because of its
    *receiver*, and `row.password` is one because of its *attribute*. Taking
    only one of the two missed whichever spelling the caller happened to use.
    """
    names: list[str] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            names.append(inner.id)
        elif isinstance(inner, ast.Attribute):
            names.append(inner.attr)
    return names


def _interpolated(node: ast.AST) -> list[ast.AST]:
    """The run-time expressions a formatted string will render.

    A constant is not one of them, which is what keeps a rule about *values*
    off a message that merely names the thing it is talking about.
    """
    if isinstance(node, ast.JoinedStr):
        return [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return [node.right]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [side for side in (node.left, node.right) if not isinstance(side, ast.Constant)]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return [*node.args, *(keyword.value for keyword in node.keywords)]
    return []


def _is_logging_call(node: ast.Call) -> bool:
    """Whether this call writes a log record."""
    callee = node.func
    if not isinstance(callee, ast.Attribute) or callee.attr not in _LOG_LEVELS:
        return False
    parts = _dotted(callee).split(".")[:-1] or [_name_of(callee.value)]
    return any(_is_logger_name(part) for part in parts if part)


def _refuses_authorization(node: ast.AST) -> bool:
    """Whether this function refuses a request on authorization grounds.

    The precondition for `authz-fail-open`, and the whole of its precision: a
    function that never refuses has no open exit to find, which is what keeps
    the rule off every `get_or_none`.
    """
    for inner in ast.walk(node):
        match inner:
            case ast.Raise(exc=ast.expr() as raised):
                pass
            case _:
                continue
        name = _name_of(raised).lower().replace("_", "")
        if any(word in name for word in _AUTHZ_EXCEPTIONS):
            return True
        if isinstance(raised, ast.Call):
            for argument in (*raised.args, *(k.value for k in raised.keywords)):
                if isinstance(argument, ast.Constant) and argument.value in _REFUSAL_STATUSES:
                    return True
    return False


def _is_undecided_test(node: ast.AST) -> bool:
    """Whether this condition means "the value could not be resolved"."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    if isinstance(node, ast.Compare):
        return any(isinstance(op, ast.Is) for op in node.ops) and any(
            isinstance(comparator, ast.Constant) and comparator.value is None
            for comparator in node.comparators
        )
    if isinstance(node, ast.BoolOp):
        return any(_is_undecided_test(value) for value in node.values)
    return False


def _is_open_return(node: ast.AST) -> bool:
    """Whether this statement leaves without deciding anything.

    `return`, `return None`, and the empty containers -- an empty filter is not
    a restriction, it is every row.
    """
    if not isinstance(node, ast.Return):
        return False
    value = node.value
    if value is None:
        return True
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    return isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple)) and not (
        value.keys if isinstance(value, ast.Dict) else value.elts
    )


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    """`except:`, `except Exception:`, `except BaseException:`."""
    if handler.type is None:
        return True
    return _name_of(handler.type) in ("Exception", "BaseException")


#: Characters a whole value is made of. A left operand outside them is a syntax
#: fragment rather than something being matched.
_VALUE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/")


def _is_whole_value(node: ast.AST) -> bool:
    """Whether the left of an `in` is a value, rather than a syntax fragment.

    Searching a route *template* for a brace, a backslash or a percent escape is
    a lexical check on a pattern the application wrote, not a security decision
    about a request -- and it is how every path-matching implementation is
    spelled. What the rule is about is a *rule* being tested for containment in
    a subject: a name, or a literal that could be a whole value on its own.
    """
    if not isinstance(node, ast.Constant):
        return True
    if not isinstance(node.value, str):
        return False
    return len(node.value) >= _MEMBERSHIP_LITERAL_LENGTH and set(node.value) <= _VALUE_CHARACTERS


def _debug_gated(scope: ast.AST, target: ast.AST) -> bool:
    """Whether `target` sits under an `if ...debug...:` inside `scope`."""
    for inner in ast.walk(scope):
        if not isinstance(inner, ast.If):
            continue
        if not any(_mentions(name, ("debug",)) for name in _expression_names(inner.test)):
            continue
        if any(node is target for statement in inner.body for node in ast.walk(statement)):
            return True
    return False


def _verifier_call(body: list[ast.stmt]) -> str:
    """The name of the credential check this block performs, if it performs one."""
    for statement in body:
        for inner in ast.walk(statement):
            if isinstance(inner, ast.Call):
                name = _name_of(inner.func)
                if _mentions(name, _VERIFIER_CALLS):
                    return name
    return ""


def _mentions_disable(node: ast.AST) -> bool:
    """Whether this condition reads a flag that switches authentication off."""
    return any(_mentions(name, _AUTH_DISABLE_WORDS) for name in _expression_names(node))


def _annotates_str(annotation: ast.AST | None) -> bool:
    """Whether this annotation says the value is a string.

    `str`, `str | None` and `Optional[str]` all count; a `list[str]` does not,
    because membership against a list of strings is the correct spelling this
    rule must stay quiet on.
    """
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name):
        return annotation.id == "str"
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.replace(" ", "").split("|")[0] == "str"
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotates_str(annotation.left) or _annotates_str(annotation.right)
    if isinstance(annotation, ast.Subscript) and _name_of(annotation.value) == "Optional":
        return _annotates_str(annotation.slice)
    return False


def _bound_names(target: ast.AST) -> list[str]:
    """The names an assignment target binds.

    Only plain names and the elements of a tuple or list unpacking. **Not the
    base of an attribute or subscript**: `self.table = value` binds an
    attribute, not `self`, and collecting it with `ast.walk` marked every
    object whose field was ever assigned from request data as caller-controlled
    -- nine of the twenty-nine false positives against Wreath's own source.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


def _name_of(node: ast.AST) -> str:
    """A readable name for an expression, for the vocabulary tests."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    if isinstance(node, ast.Subscript):
        return _name_of(node.value)
    return ""


def _dotted(node: ast.AST) -> str:
    """`a.b.c` for an attribute chain, else ''."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


#: Request methods that expose caller-controlled values. `request.state` is
#: excluded because it contains middleware-resolved values rather than input.
_REQUEST_READS: Final = (
    "request.json",
    "request.form",
    "request.body",
    "request.query_params",
    "request.path_params",
    "request.cookies",
    "request.headers",
    "request.header",
)


def _reads_request(node: ast.AST) -> bool:
    """Whether this expression reads anything the caller put on the request.

    Matched on the attribute rather than the call, so the subscript spellings
    (`request.query_params["q"]`) are covered by the same arm as the call ones
    (`await request.json()`), and a receiver other than the bare name --
    `self.request.headers` -- still matches on the suffix.
    """
    return any(
        isinstance(child, ast.Attribute) and _dotted(child).endswith(_REQUEST_READS)
        for child in ast.walk(node)
    )


def _is_dynamic_string(node: ast.AST) -> bool:
    """Whether this expression builds a string from something at run time."""
    if isinstance(node, ast.JoinedStr):
        # A constant f-string is a constant. Flagging it teaches nothing.
        return any(isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return any(
            isinstance(side, ast.Constant) and isinstance(side.value, str)
            for side in (node.left, node.right)
        )
    if isinstance(node, ast.Call):
        callee = node.func
        if isinstance(callee, ast.Attribute) and callee.attr in ("format", "join"):
            return True
    return False


class _Scanner(ast.NodeVisitor):
    """One walk, every rule.

    Two passes in practice: `prepare` collects the module-wide facts a rule
    needs (which names hold request data, whether a proxy trust boundary is
    configured), then the walk applies the rules. Doing it the other way round
    made a rule's answer depend on the order statements happened to appear in.
    """

    def __init__(self, tree: ast.Module, surface: str, source: str = "") -> None:
        self.tree = tree
        self.surface = surface
        self.lines = source.splitlines()
        self.findings: list[Finding] = []
        self.request_bound: set[str] = set()
        self.random_bound: set[str] = set()
        self.dynamic_strings: set[str] = set()
        self.origin_bound: set[str] = set()
        self.archive_members: set[str] = set()
        self.proxy_trusted = False
        #: Whether any outbound client in this module names a destination
        #: policy. The same "is there a boundary?" precondition `proxy_trusted`
        #: applies to forwarding headers.
        self.destination_policy = False
        #: Names the file itself declares to hold a string. What separates
        #: `"admin" in roles` -- correct, over a collection -- from the same
        #: line written against a policy string.
        self.str_names: set[str] = set()
        #: One finding per rule per line. The control-flow rules walk each
        #: function, and a nested function is walked again with its parent, so
        #: without this a factory and the closure it returns report twice.
        self._seen: set[tuple[str, str]] = set()
        #: Names whose value a caller chose: handler parameters, request data,
        #: and anything derived from them. This is what separates an injection
        #: from a schema-qualified statement.
        self.caller_controlled: set[str] = set()
        #: Names bound once at module scope to a literal. An application's
        #: shell template and its schema name both live here, and neither is
        #: something a caller chose.
        self.module_constants: set[str] = set()
        #: Names holding a `Path`, so `x / y` can be read as a join rather than
        #: as division. Collected from the whole module, because the root is
        #: almost always a module constant and the join is in a handler.
        self.path_names: set[str] = {
            target.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _name_of(node.value.func) in ("Path", "PurePath")
            for target in node.targets
            if isinstance(target, ast.Name)
        }

    def _waived(self, rule_id: str, line: int) -> bool:
        """Whether a reviewed directive already covers this line.

        The statement's own line and the two above it, because a directive on a
        multi-line call conventionally sits on the opening line and the node
        this scanner flags is sometimes an inner expression.
        """
        codes = _EQUIVALENT_NOQA.get(rule_id, ())
        for offset in (0, -1, -2, 1):
            index = line - 1 + offset
            if not (0 <= index < len(self.lines)):
                continue
            text = self.lines[index]
            if f"{_WAIVER} {rule_id}" in text:
                return True
            if "noqa" in text and any(code in text for code in codes):
                return True
        return False

    def _flag(self, rule_id: str, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        if self._waived(rule_id, line):
            return
        location = f"{line}:{getattr(node, 'col_offset', 0)}"
        if (rule_id, location) in self._seen:
            return
        self._seen.add((rule_id, location))
        rule = _BY_ID[rule_id]
        self.findings.append(
            Finding(
                rule_id=rule.rule_id,
                severity=rule.severity,
                surface=self.surface,
                message=message,
                reference=rule.reference,
                location=location,
                suggestion=rule.suggestion,
            )
        )

    def prepare(self) -> None:
        for statement in self.tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                for target in targets:
                    self.module_constants.update(_bound_names(target))
        self._collect_handler_parameters()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                dotted = _dotted(node.func)
                name = _name_of(node.func)
                if name == "ProxyPolicy" and _establishes_trust(node):
                    # Only a *real* boundary silences `untrusted-forwarded-header`.
                    # `trusted=["*"]` trusts every peer, which is no boundary at
                    # all, and treating it as one meant the worst possible
                    # spelling bought the quietest possible result.
                    self.proxy_trusted = True
                if name == "DestinationPolicy":
                    self.destination_policy = True
                if (
                    dotted.endswith("infolist")
                    or dotted.endswith("namelist")
                    or dotted.endswith("getmembers")
                ):
                    self.archive_members.add(dotted.split(".")[0])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_string_parameters(node)
            if isinstance(node, ast.AnnAssign) and _annotates_str(node.annotation):
                self.str_names.update(_bound_names(node.target))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._bind(node)
            if isinstance(node, ast.For):
                # `for member in archive.infolist():` binds the member name.
                iterated = node.iter
                if isinstance(iterated, ast.Call):
                    dotted = _dotted(iterated.func)
                    if dotted.split(".")[-1] in ("infolist", "namelist", "getmembers"):
                        for target in ast.walk(node.target):
                            if isinstance(target, ast.Name):
                                self.archive_members.add(target.id)
        self._propagate()

    def _collect_handler_parameters(self) -> None:
        """Every parameter of every route-decorated function.

        A handler is recognisable from its decorator alone, which is why this
        needs no application object: `@router.get("/x")` and `@app.post("/y")`
        both end in an attribute from `_ROUTE_DECORATORS`. Framework-injected
        parameters are excluded -- a `Session` is not caller-controlled, and
        treating it as such put a finding on every handler that runs a query.
        """
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_route_decorator(d) for d in node.decorator_list):
                continue
            arguments = node.args
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
                if argument.arg in _INJECTED_PARAMS:
                    continue
                annotation = ast.unparse(argument.annotation) if argument.annotation else ""
                if any(marker in annotation for marker in _INJECTED_ANNOTATIONS):
                    continue
                self.caller_controlled.add(argument.arg)

    def _collect_string_parameters(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Parameters the signature declares to be a string.

        The provenance signal for `substring-security-match`. Deciding on what
        the file *says* rather than on what the expression looks like is the
        same move `sql-interpolation` makes with module constants, and for the
        same reason: `"admin" in roles` and `"admin" in scope_string` are one
        shape and two different pieces of code.
        """
        arguments = node.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            if _annotates_str(argument.annotation):
                self.str_names.add(argument.arg)

    def _propagate(self) -> None:
        """One hop at a time until nothing new is caller-controlled.

        A fixed point rather than a single pass, because `needle = q.strip()`
        followed by `sql = f"...{needle}..."` needs two, and the statements are
        not guaranteed to be walked in that order.
        """
        for _ in range(8):  # bounded; real chains are short
            before = len(self.caller_controlled) + len(self.dynamic_strings)
            for node in ast.walk(self.tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [name for target in targets for name in _bound_names(target)]
                if self._tainted(value):
                    self.caller_controlled.update(names)
            if len(self.caller_controlled) + len(self.dynamic_strings) == before:
                return

    def _bind(self, node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None:
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [name for target in targets for name in _bound_names(target)]
        source = value.value if isinstance(value, ast.Await) else value

        if self._is_request_data(source):
            self.request_bound.update(names)
        if self._is_random(source):
            self.random_bound.update(names)
        if _is_dynamic_string(source):
            self.dynamic_strings.update(names)
        if self._is_origin_read(source):
            self.origin_bound.update(names)

    def _is_request_data(self, node: ast.AST) -> bool:
        return _reads_request(node)

    def _tainted(self, node: ast.AST) -> bool:
        """Whether this expression carries anything the caller chose.

        The two ways it can: a name already known to be caller-controlled, or a
        read off the request in place. The second half is why this is a method
        rather than eight copies of one set intersection -- the copies had it
        missing, uniformly, and a rule that catches the bound-parameter spelling
        and not the `request.query_params` one catches neither in practice.
        """
        referenced = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if referenced & (self.caller_controlled | self.request_bound):
            return True
        return _reads_request(node)

    def _is_random(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted.startswith("random.") or dotted == "random":
                return True
            # `random.randbytes(16).hex()` needs no arm here, though it looks as
            # though it should: `_dotted` gives up on a chained call and returns
            # "", but `_draws_on_random` -- which is what decides the finding --
            # walks every call in the expression and sees `random.randbytes`
            # one level in. An arm was written here, and a mutant removing it
            # changed nothing, which is how the redundancy was found.
            root = dotted.partition(".")[0]
            return root in self.random_bound
        return False

    def _is_origin_read(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if not _dotted(node.func).endswith(("header", "get")):
            return False
        return any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.lower() == "origin"
            for arg in node.args
        )

    def visit_Call(self, node: ast.Call) -> None:
        self._sql(node)
        self._secrets(node)
        self._ssrf(node)
        self._wildcard_trust(node)
        self._xml(node)
        self._template(node)
        self._dynamic_import(node)
        self._archive(node)
        self._cors(node)
        self._debug(node)
        self._forwarded(node)
        self._random_use(node)
        self._secret_in_log(node)
        self._outbound_url(node)
        self._path_join_call(node)
        self.generic_visit(node)

    def _path_join_call(self, node: ast.Call) -> None:
        """`os.path.join(root, name)` -- the same defect as `root / name`.

        Worse in one specific way, and it is the way that catches people: an
        absolute component does not append to the root, it *replaces* it, and
        `os.path.join("/srv/exports", "/etc/passwd")` is `/etc/passwd` with no
        error and no `..` anywhere in the input.
        """
        if _dotted(node.func) not in ("os.path.join", "path.join", "posixpath.join"):
            return
        for argument in node.args[1:]:
            if self._tainted(argument):
                self._flag(
                    "path-from-request",
                    node,
                    "a path is joined with a value the caller chose; an absolute "
                    "component replaces the root rather than extending it",
                )
                return

    def _secret_in_log(self, node: ast.Call) -> None:
        """A credential, or a caller's own body, formatted into a log record.

        Debug logging is off in production until the day somebody turns it on
        to investigate an incident, which is the day the value leaves the
        database's retention policy for the log aggregator's -- a different
        audience, a different lifetime, and usually a different jurisdiction.

        The rule is about the *value*, so only interpolated expressions count.
        Naming a secret in the message is a label and stays quiet.
        """
        if not _is_logging_call(node):
            return
        candidates: list[ast.AST] = []
        for argument in node.args:
            rendered = _interpolated(argument)
            if rendered:
                candidates.extend(rendered)
            else:
                candidates.append(argument)
        # Every keyword too. `extra=` is how structured logging carries fields,
        # and the rest -- `exc_info`, `stacklevel` -- hold nothing a credential
        # vocabulary matches, so narrowing to one name bought nothing.
        candidates.extend(keyword.value for keyword in node.keywords)
        for expression in candidates:
            secret = next(
                (name for name in _expression_names(expression) if _is_credential_name(name)),
                None,
            )
            if secret:
                self._flag("secret-in-log", node, f"{secret} is formatted into a log record")
                return
            referenced = {n.id for n in ast.walk(expression) if isinstance(n, ast.Name)}
            supplied = referenced & self.request_bound
            if supplied or _reads_request(expression):
                named = sorted(supplied)[0] if supplied else "a value read off the request"
                self._flag(
                    "secret-in-log",
                    node,
                    f"{named} is the caller's own body, which is not known to be "
                    "free of credentials",
                )
                return

    def _outbound_url(self, node: ast.Call) -> None:
        """A request whose destination the caller chose.

        Checking the string once is not a fix, which is why the remediation is
        a policy rather than a validator: the redirect target and every address
        DNS returns are the destination too.
        """
        if self.destination_policy:
            return
        callee = node.func
        if not isinstance(callee, ast.Attribute) or callee.attr not in _HTTP_VERBS:
            return
        keyed = next((k.value for k in node.keywords if k.arg == "url"), None)
        if keyed is not None:
            target = keyed
        else:
            receiver = _dotted(callee).split(".")[:-1] or [_name_of(callee.value)]
            # `.get` is `dict.get` far more often than it is an HTTP verb, and
            # the caller's own body is the most common receiver of both. The
            # receiver is what decides, not the verb.
            if not any(_mentions(part, _HTTP_CLIENT_NAMES) for part in receiver):
                return
            target = node.args[0] if node.args else None
        if target is None:
            return
        referenced = {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        chosen = referenced & (self.caller_controlled | self.request_bound)
        if chosen or _reads_request(target):
            named = sorted(chosen)[0] if chosen else "the request"
            self._flag(
                "outbound-url-from-request",
                node,
                f"the destination of this request comes from {named}",
            )

    def _sql(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _SQL_SINKS:
            return
        if not node.args:
            return
        first = node.args[0]
        if isinstance(first, ast.Name):
            interpolated = first.id in self.dynamic_strings
            tainted = first.id in self.caller_controlled
        else:
            interpolated = _is_dynamic_string(first)
            tainted = self._tainted(first)
        # Interpolating a module constant is how a schema-qualified statement is
        # written. Interpolating something a caller chose is an injection. Only
        # the second is a finding, and conflating them is what made the first
        # draft of this rule unusable.
        if interpolated and tainted:
            self._flag(
                "sql-interpolation",
                node,
                f"SQL passed to .{node.func.attr}() is built by string interpolation",
            )

    def _secrets(self, node: ast.Call) -> None:
        callee = _name_of(node.func)
        if callee in _POSITIONAL_SECRET_CALLEES and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, (str, bytes)):
                self._flag(
                    "hardcoded-secret",
                    node,
                    f"{callee} is constructed with a literal signing key",
                )
        for keyword in node.keywords:
            if keyword.arg in _SECRET_KEYWORDS and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, (str, bytes)) and keyword.value.value:
                    self._flag(
                        "hardcoded-secret",
                        node,
                        f"{keyword.arg}= is a literal",
                    )

    def _wildcard_trust(self, node: ast.Call) -> None:
        credentialed = any(
            keyword.arg == "allow_credentials"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        for keyword in node.keywords:
            if not _wildcard_in(keyword.value):
                continue
            if keyword.arg in _TRUST_KEYWORDS:
                self._flag(
                    "wildcard-trust-list",
                    node,
                    f"{keyword.arg}= accepts every peer",
                )
            elif keyword.arg in _ORIGIN_KEYWORDS and credentialed:
                # A public read-only API may answer any origin. Credentials are
                # what turn the wildcard into a trust decision, and that single
                # pair is the one `CorsPolicy` refuses at construction.
                self._flag(
                    "wildcard-trust-list",
                    node,
                    f"{keyword.arg}= is a wildcard alongside allow_credentials=True",
                )

    def _ssrf(self, node: ast.Call) -> None:
        if _name_of(node.func) != "DestinationPolicy":
            return
        widened = [
            keyword.arg
            for keyword in node.keywords
            if keyword.arg in ("allow_private", "allow_loopback", "allow_link_local")
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
        ]
        if widened:
            self._flag(
                "ssrf-policy-widened",
                node,
                "DestinationPolicy permits " + ", ".join(sorted(widened)),
            )

    def _xml(self, node: ast.Call) -> None:
        if _name_of(node.func) != "setFeature":
            return
        for arg in node.args:
            name = _name_of(arg)
            if name.startswith("feature_external"):
                self._flag(
                    "unsafe-xml-parser",
                    node,
                    f"{name} is enabled, which lets the document name files and URLs",
                )

    def _template(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "from_string":
            return
        first = node.args[0] if node.args else None
        constant = (
            isinstance(first, ast.Constant)
            or (isinstance(first, ast.Name) and first.id in self.module_constants)
            or (
                # `_SHELL % {...}` -- a constant formatted with constants.
                isinstance(first, ast.BinOp)
                and isinstance(first.left, ast.Name)
                and first.left.id in self.module_constants
            )
        )
        if first is not None and not constant:
            self._flag(
                "template-from-request",
                node,
                "a template is compiled from a value that is not a literal",
            )

    def _dynamic_import(self, node: ast.Call) -> None:
        callee = _name_of(node.func)
        dotted = _dotted(node.func)
        if callee in ("eval", "exec") and not isinstance(node.func, ast.Attribute):
            self._flag("dynamic-import", node, f"{callee}() executes data as code")
            return
        if dotted.endswith("import_module") or callee == "__import__":
            caller_chose = bool(node.args) and self._tainted(node.args[0])
            if node.args and not isinstance(node.args[0], ast.Constant) and caller_chose:
                self._flag(
                    "dynamic-import",
                    node,
                    "a module name is resolved from a value that is not a literal",
                )

    def _archive(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "extractall":
            self._flag(
                "unsafe-archive-extract",
                node,
                "extractall() honours member paths and symlinks as given",
            )

    def _cors(self, node: ast.Call) -> None:
        literals = [
            arg
            for arg in ast.walk(node)
            if isinstance(arg, ast.Constant)
            and isinstance(arg.value, (str, bytes))
            and _as_text(arg.value) == "access-control-allow-origin"
        ]
        if not literals:
            return
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if names & self.origin_bound or any(_mentions(n, ("origin",)) for n in names):
            self._flag(
                "cors-reflect-origin",
                node,
                "Access-Control-Allow-Origin is set from the request's own Origin",
            )

    def _debug(self, node: ast.Call) -> None:
        if _name_of(node.func) != "Wreath":
            return
        for keyword in node.keywords:
            if (
                keyword.arg == "debug"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self._flag("debug-enabled", node, "the application is constructed with debug=True")

    def _forwarded(self, node: ast.Call) -> None:
        if self.proxy_trusted:
            return
        if not _dotted(node.func).endswith(("header", "get")):
            return
        for arg in node.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.lower() in _FORWARDED_HEADERS
            ):
                self._flag(
                    "untrusted-forwarded-header",
                    node,
                    f"{arg.value} is read but no ProxyPolicy establishes it",
                )

    def _random_use(self, node: ast.Call) -> None:
        """`random.Random(x)` seeded from a value is a defect on its own."""
        if _dotted(node.func) == "random.Random" and node.args:
            argument = node.args[0]
            # A module constant is a *reproducibility* seed -- a data seeder, a
            # property test. Both of the example application's findings were
            # this, and neither was a security draw.
            fixed = isinstance(argument, ast.Constant) or (
                isinstance(argument, ast.Name) and argument.id in self.module_constants
            )
            if not fixed:
                self._flag(
                    "weak-randomness",
                    node,
                    "random.Random is seeded from a value, so every draw is reproducible",
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [name for target in node.targets for name in _bound_names(target)]
        if any(_is_secret_name(name) for name in names) and self._draws_on_random(node.value):
            self._flag(
                "weak-randomness",
                node,
                f"{names[0]} is drawn from random rather than secrets",
            )
        self._declared_secret(names, node)
        self._env_conditional(names, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        names = _bound_names(node.target)
        self._declared_secret(names, node)
        self._env_conditional(names, node)
        self.generic_visit(node)

    def _declared_secret(self, names: list[str], node: ast.Assign | ast.AnnAssign) -> None:
        """A signing key written as the value of a declaration.

        The shape a key actually ships in: a default on a settings field, so
        every deployment that has not set the environment variable signs with
        what is in the source, and nothing complains at startup because from
        the application's point of view the setting is populated.

        `_looks_like_a_key` is what keeps this off the declarations that share
        the vocabulary and carry no secret -- an algorithm name, a state key, a
        sentinel, a URN. Those are identifiers, and they do not look like keys.
        """
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, (str, bytes)):
            return
        if not _looks_like_a_key(value.value):
            return
        for name in names:
            if _is_credential_name(name):
                self._flag(
                    "hardcoded-secret",
                    node,
                    f"{name} is declared with a literal value",
                )
                return

    def _env_conditional(self, names: list[str], node: ast.Assign | ast.AnnAssign) -> None:
        """A security control whose strength depends on which environment runs.

        Every review reads "secure in production" and stops. Nobody rereads it
        when a new environment name appears, when a staging deployment is
        pointed at real data, or when the variable is simply unset -- and the
        unset default is the weak arm, because that is the one a developer
        needed.
        """
        if not any(_is_security_flag(name) for name in names):
            return
        value = node.value
        if isinstance(value, ast.IfExp):
            decider: ast.AST = value.test
        elif isinstance(value, ast.Compare):
            decider = value
        else:
            return
        if any(_mentions(name, _ENVIRONMENT_WORDS) for name in _expression_names(decider)):
            self._flag(
                "env-conditional-security",
                node,
                f"{names[0]} is decided by which environment is running",
            )

    def _draws_on_random(self, node: ast.AST) -> bool:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                dotted = _dotted(inner.func)
                if dotted.startswith("random."):
                    return True
                root = dotted.partition(".")[0]
                if root in self.random_bound:
                    return True
        return False

    def visit_Compare(self, node: ast.Compare) -> None:
        self._timing(node)
        self._case_mapped(node)
        self._substring_match(node)
        self.generic_visit(node)

    def _substring_match(self, node: ast.Compare) -> None:
        """A security decision made by substring, so a longer value satisfies a
        shorter rule.

        Two spellings, one class. Against a string *literal* it is usually a
        one-element tuple that lost its comma, which both over-matches and
        reads as correct because it does happen to match the value it was
        written for. Against a value the file declares to be a string it is a
        policy or a path being tested for containment, where a route merely
        *containing* an exempted segment is exempt.
        """
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            return
        subject = node.comparators[0]
        if isinstance(subject, ast.Constant) and isinstance(subject.value, str):
            if len(subject.value) < _MEMBERSHIP_LITERAL_LENGTH:
                return
            if any(_mentions(name, _MATCH_CONTEXT_WORDS) for name in _expression_names(node.left)):
                self._flag(
                    "substring-security-match",
                    node,
                    f'this tests for a substring of "{subject.value}", not equality with it',
                )
            return
        # Deciding on what the file *says* the value is, rather than on what the
        # expression looks like: `"admin" in roles` over a collection is correct
        # code, and nothing distinguishes it by shape alone.
        if (
            isinstance(subject, ast.Name)
            and subject.id in self.str_names
            and _mentions(subject.id, _MATCH_SUBJECT_WORDS)
            and _is_whole_value(node.left)
        ):
            self._flag(
                "substring-security-match",
                node,
                f"{subject.id} is a string, so this is a containment test rather than a match",
            )

    def _timing(self, node: ast.Compare) -> None:
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            return
        operands = [node.left, *node.comparators]
        if any(isinstance(operand, ast.Constant) for operand in operands):
            # `alg == "RS256"`, `token_type == "bearer"`. Comparing against a
            # known literal is a dispatch, not a secret check, and flagging it
            # accounted for most of this rule's false positives.
            return
        names = [_name_of(operand) for operand in operands]
        strong = any(_is_secret_name(name) for name in names)
        weak = any(_is_weak_secret_name(name) for name in names) and any(
            _mentions(name, _COMPARISON_ROLES) for name in names
        )
        # The provenance spelling of the same second signal. A double-submit
        # check writes `cookie_token == header_token`, where neither operand
        # names a comparison role but the pair says exactly what the role words
        # say: a presented value is being checked against a held one.
        provenances = [_provenance(name) for name in names]
        paired = (
            all(_is_weak_secret_name(name) for name in names)
            and all(provenances)
            and provenances[0] != provenances[1]
        )
        if strong or weak or paired:
            self._flag(
                "timing-unsafe-compare",
                node,
                "a secret is compared with == , which returns on the first wrong byte",
            )

    def _case_mapped(self, node: ast.Compare) -> None:
        left = node.left
        mapped = (
            isinstance(left, ast.Call)
            and isinstance(left.func, ast.Attribute)
            and left.func.attr in _CASE_MAPPINGS
        )
        if not mapped:
            return
        if not _mentions(_name_of(left.func.value), _IDENTITY_WORDS):  # type: ignore[union-attr]
            return
        if any(_mentions(_name_of(operand), _AUTHZ_WORDS) for operand in node.comparators):
            self._flag(
                "case-mapped-authz",
                node,
                f".{left.func.attr}() is applied before an authorization comparison",  # type: ignore[union-attr]
            )

    # The rules above decide an expression. These decide a *branch*, which
    # needs one fact the expression rules do not: what the enclosing function
    # is for. A `return None` is unremarkable in a lookup and is an open door
    # in an authorizer, and only the function around it says which this is.

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)
        self.generic_visit(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if _refuses_authorization(node):
            self._fail_open(node)
        self._auth_fallback(node, authentication=_mentions(node.name, _AUTHENTICATION_FUNCTIONS))

    def _fail_open(self, node: ast.AST) -> None:
        """An authorization check that returns when it cannot decide.

        The defect is not the `return`; it is that the undecidable case and the
        permitted case are spelled identically, so no caller can tell them
        apart. The comment on that branch is always some version of "the
        handler will sort it out", and the handler never does -- what reaches
        it is an absent restriction, which is every row.

        The precondition is the whole of the precision: a function that never
        refuses has no open exit to find, which is what keeps this off every
        `get_or_none`.
        """
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If) or not _is_undecided_test(inner.test):
                continue
            for statement in inner.body:
                if _is_open_return(statement):
                    self._flag(
                        "authz-fail-open",
                        statement,
                        "this leaves the check without deciding, so an unresolved subject is "
                        "indistinguishable from a permitted one",
                    )
                    break

    def _auth_fallback(self, node: ast.AST, *, authentication: bool) -> None:
        """A broad handler that retries, in a path that decides who the caller is.

        Two defects share this shape and either one is enough. The path
        degrades to a *weaker* verifier when the strong one raises, so anything
        that can make the strong path fail selects the weak one. And a refusal
        raised inside the `try` is caught by the same handler, which converts an
        explicit denial into a retry rather than a 401.

        A handler that re-raises keeps one exit and is not this.
        """
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Try):
                continue
            for handler in inner.handlers:
                if not _is_broad_handler(handler):
                    continue
                if any(isinstance(statement, ast.Raise) for statement in handler.body):
                    continue
                verifier = _verifier_call(handler.body)
                if authentication and verifier:
                    self._flag(
                        "auth-fallback-on-exception",
                        handler,
                        f"a failed verification falls through to {verifier}()",
                    )
                elif _refuses_authorization(ast.Module(body=inner.body, type_ignores=[])):
                    self._flag(
                        "auth-fallback-on-exception",
                        handler,
                        "a refusal raised in this block is caught here, so a denial becomes a "
                        "retry rather than a refusal",
                    )

    def visit_If(self, node: ast.If) -> None:
        if _mentions_disable(node.test):
            for statement in node.body:
                if isinstance(statement, ast.Return):
                    self._flag(
                        "auth-disable-flag",
                        node,
                        "a configuration flag short-circuits this check",
                    )
                    break
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        if _mentions_disable(node.test):
            self._flag(
                "auth-disable-flag",
                node,
                "a configuration flag decides whether this control is applied",
            )
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._error_detail(node)
        self.generic_visit(node)

    def _error_detail(self, node: ast.Try) -> None:
        """A caught exception's own text returned to the caller.

        The message names the schema, the statement, sometimes the parameters.
        It is written for one debugging session and it is never removed.

        **Only a broad handler.** If you named the type you caught, you know
        what its message says and you wrote it -- `except _ProtobufDecodeError
        as exc: raise BadRequest(f"invalid protobuf body: {exc}")` is a refusal
        addressed to the caller, and `AGENTS.md` asks for exactly that narrow
        catch. It is the broad one that cannot know what it holds, which is the
        same reason it is the exception rather than the rule.
        """
        for handler in node.handlers:
            if not handler.name or not _is_broad_handler(handler):
                continue
            for inner in ast.walk(handler):
                if not isinstance(inner, ast.Call) or _is_logging_call(inner):
                    continue
                if _debug_gated(handler, inner):
                    # Disclosure behind a debug flag is a decision somebody
                    # made and can see. Shipping the flag on is the finding,
                    # and `debug-enabled` is the rule that reports it.
                    continue
                responder = _name_of(inner.func).lower().replace("_", "")
                if responder not in _RESPONSE_CALLEES:
                    # A result object with an `error=` field is not a response.
                    continue
                candidates = [k.value for k in inner.keywords if k.arg in _RESPONSE_TEXT_KEYWORDS]
                candidates.extend(inner.args)
                for candidate in candidates:
                    if any(name == handler.name for name in _expression_names(candidate)):
                        self._flag(
                            "error-detail-leaked",
                            inner,
                            f"the caught exception's own text is returned to the caller "
                            f"as {responder or 'the response'} content",
                        )
                        return

    def visit_For(self, node: ast.For) -> None:
        self._mass_assignment(node)
        self._archive_member_path(node)
        self._elementwise_compare(node)
        self.generic_visit(node)

    def _elementwise_compare(self, node: ast.For) -> None:
        """A comparison hand-rolled as a loop that stops at the first difference.

        `_timing` decides an `ast.Compare` between two named operands, and this
        shape has neither: the operands are `a` and `b`, and the secret is the
        sequence being iterated. It is the same defect written out longhand, and
        it is the spelling that survives review -- a loop looks like work, where
        `==` looks like a comparison somebody might question.

        Matched on structure alone: iterate a pair, compare the two elements,
        leave the function on the first mismatch. Nothing correct is written
        that way, because the correct version does not return early.
        """
        if not isinstance(node.iter, ast.Call) or _name_of(node.iter.func) != "zip":
            return
        # No arity check on `elements`: `operands <= elements` below is stricter
        # than one. A single-name loop can only satisfy it by comparing an
        # element with itself, which is not a shape anybody writes -- a mutant
        # removing the arity check went unnoticed, and that is why it is gone.
        elements = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        for statement in node.body:
            if not isinstance(statement, ast.If) or not isinstance(statement.test, ast.Compare):
                continue
            test = statement.test
            operands = {_name_of(operand) for operand in (test.left, *test.comparators)}
            if not operands <= elements or not any(
                isinstance(op, (ast.Eq, ast.NotEq)) for op in test.ops
            ):
                continue
            if any(isinstance(inner, ast.Return) for inner in statement.body):
                self._flag(
                    "timing-unsafe-compare",
                    test,
                    "this loop returns at the first difference, so how long it "
                    "runs says how much of the secret was right",
                )
                return

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self._path_from_request(node)
        self.generic_visit(node)

    def _path_from_request(self, node: ast.BinOp) -> None:
        """`root / name`, where the caller chose `name`.

        `Path.__truediv__` is not containment and does not pretend to be: an
        absolute right-hand side discards the root entirely, and `..` walks out
        of it. The line reads as though the root bounds the result, which is
        exactly why it survives -- the containment is *implied by the shape* and
        implemented nowhere.

        Keyed on the taint sets rather than on the name, so `root / "manifest"`
        and `root / settings.EXPORT_NAME` stay quiet. Only a component the
        caller supplied is a traversal.
        """
        if not isinstance(node.op, ast.Div) or not self._is_pathlike(node.left):
            return
        if not self._tainted(node.right):
            return
        self._flag(
            "path-from-request",
            node,
            "a path is joined with a value the caller chose, which `/` does not contain",
        )

    def _is_pathlike(self, node: ast.AST) -> bool:
        """Whether this expression is a path, so `/` means a join rather than division."""
        if isinstance(node, ast.Call) and _name_of(node.func) in ("Path", "PurePath"):
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_pathlike(node.left)
        name = _name_of(node)
        return bool(name) and (_mentions(name, _PATH_ROOT_WORDS) or name in self.path_names)

    def _mass_assignment(self, node: ast.For) -> None:
        # A bound body model is the same value as `await request.json()` with a
        # schema in front of it, and walking *it* onto a row throws the schema
        # away again -- so the caller-controlled set counts here too.
        if not self._tainted(node.iter):
            return
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _name_of(inner.func) == "setattr":
                self._flag(
                    "mass-assignment",
                    inner,
                    "every key of the request body is written onto the object",
                )
                return

    def _archive_member_path(self, node: ast.For) -> None:
        """A member's own name used to build a destination path.

        This is the hand-rolled extractor -- the loop `extractall` was avoided
        in favour of -- and its provenance is visible in one expression, which
        is why it can be matched precisely where general traversal cannot.
        """
        members = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        members |= self.archive_members
        for inner in ast.walk(node):
            if not isinstance(inner, ast.BinOp) or not isinstance(inner.op, ast.Div):
                continue
            for side in (inner.left, inner.right):
                if (
                    isinstance(side, ast.Attribute)
                    and side.attr in ("filename", "name")
                    and _name_of(side.value) in members
                ):
                    self._flag(
                        "unsafe-archive-extract",
                        inner,
                        "a destination path is built from an archive member's own name",
                    )
                    return

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith(_UNSAFE_XML_MODULES):
                self._flag(
                    "unsafe-xml-parser",
                    node,
                    f"{alias.name} resolves external entities unless explicitly disabled",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.startswith(_UNSAFE_XML_MODULES):
            self._flag(
                "unsafe-xml-parser",
                node,
                f"{node.module} resolves external entities unless explicitly disabled",
            )
        self.generic_visit(node)


def _as_text(value: str | bytes) -> str:
    return (value.decode("latin-1") if isinstance(value, bytes) else value).lower()


def scan_source(source: str, *, surface: str) -> list[Finding]:
    """Apply every code rule to one module's source.

    `source` is dedented first so a snippet lifted out of a docstring or a test
    parses the same way it would as a file.

    A file that does not parse yields a single `unparseable` finding rather than
    raising: one bad file in a tree must not take the scan of the other two
    hundred with it.
    """
    text = textwrap.dedent(source)
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        rule = _BY_ID["unparseable"]
        return [
            Finding(
                rule_id=rule.rule_id,
                severity=rule.severity,
                surface=surface,
                message=f"could not parse: {error.msg}",
                reference=rule.reference,
                location=f"{error.lineno or 0}:{error.offset or 0}",
                suggestion=rule.suggestion,
            )
        ]
    scanner = _Scanner(tree, surface, text)
    scanner.prepare()
    scanner.visit(tree)
    return scanner.findings
