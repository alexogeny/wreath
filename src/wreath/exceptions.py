"""Structured HTTP exceptions.

Raise one of these from a handler, a dependency, a middleware hook, or an
authentication backend and the application answers with an RFC 9457
`application/problem+json` document -- never `{"detail": ...}`. `raise NotFound()`
answers with a 404 whose body is
`{"type":"about:blank","title":"Not Found","status":404,"detail":"NotFound"}`.

The status is a class attribute, not a constructor argument, so every status
wreath raises has a type that application code can catch and that
`Wreath.exception_handler()` can register against. `Wreath.add_status_handler()`
registers against the status instead, and either intercepts the exception before
the problem document is built.
"""

from __future__ import annotations

from collections.abc import Iterable


class HTTPException(Exception):
    """The base of every status-carrying exception, and a 500 on its own.

    Carries the two things the error boundary needs: `status`, a class attribute
    each subclass overrides, and `detail`, the per-occurrence explanation that
    becomes the problem document's `detail` member. An empty `detail` falls back
    to the exception class's own name, so `raise NotFound()` still produces a
    document with a `detail` of `NotFound` rather than an empty string; the
    document's `title` comes from the status phrase either way.

    `headers` is stored as a tuple and copied onto the problem response, which is
    how a 401 carries its challenge and a 429 its `Retry-After`. It is used as
    given: a `content-type` among them replaces `application/problem+json` while
    the body stays a problem document, so do not put one there. Nothing is copied
    at all when a registered exception or status handler answers in place of the
    built-in problem response -- that handler owns the whole response.

    Args:
        detail: Explanation of this occurrence; the class name when empty.
        headers: Header pairs copied onto the response; names lowercase, as ASGI expects.
    """

    status = 500

    def __init__(
        self,
        detail: str = "",
        *,
        headers: Iterable[tuple[bytes, bytes]] = (),
    ) -> None:
        self.detail = detail or type(self).__name__
        self.headers = tuple(headers)
        super().__init__(self.detail)


class BadRequest(HTTPException):
    """400 Bad Request -- the request is malformed and will not be understood.

    Takes the same `detail` and `headers` as `HTTPException`, and answers with
    the same problem+json document under a 400 status.
    """

    status = 400


class ClientDisconnect(BadRequest):
    """400 -- the peer went away before the request body finished arriving.

    A 4xx because the request never completed; distinct from `BadRequest` so an
    application can tell "malformed" from "never finished" in a handler or a
    log, and so the response -- which usually has nowhere to go -- is not
    counted as a server fault. Raised by the request-body readers, not by
    application code; catch it to abandon work whose result no one will read.
    """


class Unauthorized(HTTPException):
    """401 Unauthorized -- no credentials, or credentials that did not verify.

    Sends a `www-authenticate` header alongside the problem document, because
    RFC 9110 §15.5.2 requires a 401 to carry a challenge. The default is
    `Bearer`; pass a scheme-specific string (`Basic realm="admin"`) to refine
    it, or `challenge=None` when a later layer supplies the header itself -- in
    which case nothing here supplies one. `detail` defaults to `Unauthorized`.

    Args:
        challenge: The `www-authenticate` value; None emits no challenge header.
    """

    status = 401

    def __init__(self, detail: str = "Unauthorized", *, challenge: str | None = "Bearer") -> None:
        # RFC 9110 15.5.2: a 401 MUST carry a WWW-Authenticate challenge, so the
        # default is a conformant one; pass a scheme-specific challenge to refine
        # it, or challenge=None only if a later layer supplies the header.
        headers = () if challenge is None else ((b"www-authenticate", challenge.encode("latin-1")),)
        super().__init__(detail, headers=headers)


class Forbidden(HTTPException):
    """403 Forbidden -- the caller is known and still not allowed to do this.

    Distinct from `Unauthorized`, which means "authenticate and try again":
    re-authenticating will not change a 403, so no challenge is sent. Takes the
    same `detail` and `headers` as `HTTPException`.
    """

    status = 403


class NotFound(HTTPException):
    """404 Not Found -- no resource at this target.

    A routing miss is answered as if this had been raised, so a handler
    registered for `NotFound` -- or for status 404 -- shapes that response too.
    A request whose path matches under some *other* method is a
    `MethodNotAllowed` (405) instead, so a 404 means no route claims the path at
    all. Takes the same `detail` and `headers` as `HTTPException`.
    """

    status = 404


class MethodNotAllowed(HTTPException):
    """405 Method Not Allowed -- the target exists but not for this method.

    Sends an `allow` header listing the methods that are supported, which
    RFC 9110 §15.5.6 requires of a 405; `wreath.audit` reports a 405 without one
    as an error. Passing no `allow` therefore emits a non-conformant response.

    Dispatch raises this for you. When nothing matches a request's method but
    some other method does answer that path, the application raises it with
    `allow` already filled in from the route table -- so a routing miss is a 404
    only when no route claims the path at all. A `GET` route is listed as
    `GET, HEAD`, because dispatch answers `HEAD` from it. Raise it yourself when
    a handler, rather than the router, is what decides a method does not apply.
    `detail` defaults to `Method Not Allowed`.

    Args:
        allow: Method names for `Allow`, joined with commas in the order given.
    """

    status = 405

    def __init__(self, detail: str = "Method Not Allowed", *, allow: Iterable[str] = ()) -> None:
        # RFC 9110 15.5.6: a 405 response MUST carry an Allow header listing the
        # methods the target resource does support.
        methods = tuple(allow)
        headers = ((b"allow", ", ".join(methods).encode("latin-1")),) if methods else ()
        super().__init__(detail, headers=headers)


class Conflict(HTTPException):
    """409 Conflict -- the request contradicts the resource's current state.

    The status for a failed optimistic-concurrency check, a duplicate unique
    key, or a state machine refusing a transition. Takes the same `detail` and
    `headers` as `HTTPException`.
    """

    status = 409


class PayloadTooLarge(HTTPException):
    """413 Content Too Large -- the request body exceeds the configured limit.

    Raised by the request-body readers against `RequestLimits.max_body_bytes`
    and `max_form_memory_bytes` while the body is still arriving, so the limit
    bounds memory rather than merely reporting on it after the fact. Takes the
    same `detail` and `headers` as `HTTPException`.
    """

    status = 413


class RequestHeaderFieldsTooLarge(HTTPException):
    """Too many header fields, or one too large to be worth parsing.

    RFC 6585 §5. Distinct from `PayloadTooLarge` because the limit that was hit
    is on the *request line and headers*, which a client fixes differently: a
    413 says "send a smaller body", a 431 says "send fewer cookies". Raised
    against `RequestLimits.max_cookie_bytes` when the cookie header is read.
    """

    status = 431


class UnprocessableEntity(HTTPException):
    """422 Unprocessable Content -- well-formed, but semantically rejected.

    Request-binding failures do not arrive here: dispatch catches
    `ValidationError` itself and answers a 422 problem document carrying every
    `{"loc", "msg", "type"}` object in an `errors` member, shaped by
    `Wreath.set_validation_formatter()` when one is installed. Raise this for an
    application-level rejection, where there is no field list to carry.
    """

    status = 422


class TooManyRequests(HTTPException):
    """429 Too Many Requests -- the caller is over a rate limit.

    Sends `retry-after` as a delta in **seconds** when `retry_after` is given
    (RFC 9110 §10.2.3, RFC 6585 §4), which is the difference between a client
    that backs off correctly and one that retries immediately. The header is a
    MAY, so omitting it is conformant -- it is just less useful. Raise this from
    an application's own quota check; `wreath.policy.ratelimit` does not go
    through it, building its 429 problem response with the header directly.

    Args:
        retry_after: Seconds the client should wait; None sends no header.
    """

    status = 429

    def __init__(
        self, detail: str = "Too Many Requests", *, retry_after: int | None = None
    ) -> None:
        # RFC 9110 10.2.3 / RFC 6585 4: a 429 MAY tell the client how long to
        # wait via Retry-After (delta-seconds).
        headers = (
            () if retry_after is None else ((b"retry-after", str(retry_after).encode("latin-1")),)
        )
        super().__init__(detail, headers=headers)
