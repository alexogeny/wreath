"""What the caller may do, answered by the policies that will enforce it.

Every application with a real authorization model ends up maintaining it twice:
once as the policies the server evaluates, and once in the frontend as a pile
of `user.role === "editor"` checks deciding which buttons to render. The
second copy drifts, and it drifts *quietly* -- a button that should have been
hidden is only a 403 the user did not expect, which no test notices.

Wreath owns the Cedar engine and the typegen IR, so the second copy can be
deleted instead of maintained. Four surfaces, and the differences between them
are the important part:

* `declared_actions` reads the vocabulary off the routes. The actions a
  client may ask about are exactly the actions the API enforces -- there is no
  second list to keep in step, because there is no second list.
* the **manifest** (`GET /permissions/manifest`) answers *what can this user
  ever do* -- resource-type level, fetched once, revalidated with an `ETag`.
  That is what nav items, buttons, and route guards need.
* the **batch endpoint** (`POST /permissions`) answers *what can this user do
  to these specific rows*, because a Cedar decision generally depends on the
  resource and no manifest can enumerate rows.
* the **stream** (`GET /permissions/stream`) says *your manifest moved, ask
  again*. Only two things can move it, and Wreath can see both: its own policy
  set was replaced, or this user's roles were written -- which the ORM already
  announces. So the client fetches once and is told when to refetch, instead of
  polling or re-deriving on every render.

None of them is enforcement. They are hints for drawing a UI, and the policy is
evaluated again on the next real request -- so a stale manifest can only ever
draw a button that then 403s, never permit something.

**That is also why the stream is allowed to be at-most-once.** It is an
ephemeral fan-out over a connection that can drop, so a *narrowing* change may
arrive late or not at all. A late narrowing draws a button that 403s, which is
cosmetic; a late *widening* hides a button the user could have used, which is
also cosmetic. Neither can grant anything, because **enforcement stays on the
route** -- keep `@authorize` there. A stream is not a permission cache with a
push, and treating it as one would be the one way to make this unsafe.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterable, Sequence
from secrets import token_bytes
from typing import Any, cast
from weakref import WeakKeyDictionary

from .._conditional import etag_matches as _etag_matches
from .._livedoc import DEFAULT_KEEPALIVE, LiveDocument, change_stream
from ..cache import BoundedCache
from ..request import Request
from ..response import JSONResponse, ProblemResponse, Response, SSEResponse
from ..router import Router
from .requirements import (
    AuthRequirement,
    add_authenticated,
    merge_requirements,
    requirement_for,
)

__all__ = [
    "PERMISSION_CHANNEL",
    "SURFACE_ACTIONS",
    "TYPE_LEVEL_ID",
    "declared_actions",
    "permission_document",
    "permissions_router",
]

#: How a route says "I front declarations of my own".
#:
#: REST and gRPC need nothing here: a gRPC method *is* a route, so its
#: `action=` is already on the route's requirement and the route table is
#: already the whole vocabulary. MCP and GraphQL are the other shape -- **one**
#: route fronting many declarations, every tool and every field -- and that
#: route's own requirement says nothing about them.
#:
#: So an endpoint may carry this attribute, or be a bound method of an object
#: that does, and its value is a callable returning the same
#: `{resource type: actions}` mapping `declared_actions` builds. A *callable*
#: rather than a mapping, deliberately: the point of this whole module is that
#: there is no second list, and a snapshot taken at mount time would be one --
#: it would miss every tool declared after the server was mounted, which is the
#: order every MCP application is written in.
SURFACE_ACTIONS = "__wreath_declared_actions__"

#: Default bus channel carrying "this manifest moved" between workers. A valid
#: SQL identifier, because `wreath.messaging` validates channel names as one.
PERMISSION_CHANNEL = "wreath_permissions"

#: Actions are conventionally `Type::verb` (see the Cedar guide), which is
#: what lets the vocabulary be grouped by resource type without a second
#: declaration. An action without the separator is grouped under `""`.
_SEPARATOR = "::"

#: The entity id the manifest asks about, standing for "any row of this type".
#: A literal `"*"` was a real id somebody's data can hold -- and a Cedar
#: policy written for that row would then decide what the *manifest* says. The
#: NUL cannot appear in a URL path segment, a JSON string PostgreSQL will store,
#: or a Cedar entity id, so nothing can collide with it.
TYPE_LEVEL_ID = "\x00type-level"

#: How many ids one batch request may ask about. A generous UI page: the
#: endpoint exists so a table is one call, and a table nobody scrolls is not
#: 200 rows. It is a ceiling rather than a page size -- see
#: `permissions_router` for why going over it refuses rather than
#: truncates.
DEFAULT_MAX_IDS = 200

#: How many policy evaluations one request may have in flight. Only a *remote*
#: authorizer can be concurrent at all -- the built-in engine is in-process and
#: never yields -- so this is a ceiling on outbound calls, not on work. Small on
#: purpose: it exists to stop one caller's batch becoming a burst of `max_ids x
#: actions` against somebody else's authorization service. Set it to 1 for an
#: authorizer that cannot take concurrent calls carrying one request.
DEFAULT_MAX_CONCURRENCY = 8


def declared_actions(app: Any) -> dict[str, tuple[str, ...]]:
    """Resource type -> the actions this application enforces on it.

    Read off the declarations themselves, so it cannot disagree with what is
    enforced. All four protocols wreath serves land here, and the differences
    between them are only in where the declaration sits:

    * **REST** declares `@authorize(action=...)` on a route handler.
    * **gRPC** declares `action=` on a method, which *is* a route -- so it
      arrives through the same requirement, with no second path.
    * **MCP** declares `action=` on a tool, a resource or a prompt, and its
      endpoint advertises them through `SURFACE_ACTIONS`.
    * **GraphQL** declares a policy per field, and its endpoint advertises the
      schema's types the same way.

    A surface with nothing enforcing it contributes nothing, exactly as a route
    with no policy does.
    """
    found: dict[str, set[str]] = {}
    image = getattr(app, "_application_image", None)
    routes = image.routes() if image is not None else getattr(app, "_routes", ())
    requirements = image.requirements() if image is not None else None
    for index, definition in enumerate(routes):
        endpoint = getattr(definition, "endpoint", None)
        if endpoint is None:
            continue
        # `@authorize` records on the endpoint; a router can add its own. The
        # effective requirement is the merge, which is what dispatch enforces.
        requirement = (
            requirements[index]
            if requirements is not None
            else merge_requirements(
                getattr(definition, "requirement", AuthRequirement()),
                requirement_for(endpoint),
            )
        )
        for policy in requirement.policies:
            resource_type, separator, _verb = policy.action.partition(_SEPARATOR)
            found.setdefault(resource_type if separator else "", set()).add(
                policy.action
            )
        # `__self__` because an MCP endpoint is a bound method: the declarations
        # belong to the server object, and an attribute set on a bound method
        # would be discarded the moment that temporary object is.
        holder = getattr(endpoint, "__self__", endpoint)
        reader = getattr(holder, SURFACE_ACTIONS, None)
        if callable(reader):
            for resource_type, actions in reader().items():
                found.setdefault(resource_type, set()).update(actions)
    for _path, endpoint in getattr(app, "_ws_routes", ()):
        requirement = requirement_for(endpoint)
        for policy in requirement.policies:
            resource_type, separator, _verb = policy.action.partition(_SEPARATOR)
            found.setdefault(resource_type if separator else "", set()).add(
                policy.action
            )
    return {name: tuple(sorted(actions)) for name, actions in sorted(found.items())}


def _vocabulary_reader(app: Any) -> Callable[[], dict[str, tuple[str, ...]]]:
    """`declared_actions`, recomputed only when the route table moved.

    Every endpoint here needs the vocabulary on every request, and rebuilding it
    means merging each route's requirements, splitting each action, and sorting
    the result -- work whose answer cannot change unless the routes do.

    **This is a complexity argument, not a measurement.** The per-request cost
    goes from O(routes) requirement merges, string partitions, set inserts and a
    sort to O(routes) pointer comparisons with no vocabulary rebuilt. No number
    is claimed; nothing here needed an ablation to justify.

    A comparison rather than a one-off computation, because the route table is
    not settled when this is built. `permissions_router` promises the routes
    it describes may be declared *after* it is mounted; and `wreath.replay`
    swaps every route's endpoint for a stub and back again, which keeps the
    route *count* identical -- so counting would not see it. Comparing the
    definitions sees both. (Adding a route once the table has compiled is
    already refused by the router, so the window is startup-shaped, but this
    does not have to assume that.)
    """
    cached: dict[str, tuple[str, ...]] = {}
    seen: tuple[Any, ...] | None = None

    def read() -> dict[str, tuple[str, ...]]:
        nonlocal cached, seen
        routes = tuple(getattr(app, "_routes", ()))
        if routes != seen:
            cached = declared_actions(app)
            seen = routes
        return cached

    return read


class _Requirement:
    """A `PolicyRequirement`-shaped ask for one action against one resource.

    Built per evaluation rather than reusing the route's, because the route's
    resource is a callable over *its* request and this one is being asked about
    an arbitrary id.
    """

    __slots__ = ("action", "resource")

    def __init__(self, action: str, resource: Any) -> None:
        self.action = action
        self.resource = resource


def _entity(resource_type: str, identifier: str) -> Any:
    from .cedar_engine import EntityUid

    return EntityUid(resource_type, identifier)


def _private(response: Response) -> Response:
    """Mark a response as this principal's alone.

    Per-principal by definition: a shared cache replaying it to the next caller
    would hand one user another's permissions. The existing `cache-control` is
    *replaced* rather than appended to -- two of them leaves a proxy reading the
    first, which was whatever the handler happened to set -- and `Vary` names
    the header the answer actually depends on, so a cache that ignores
    `private` still keys on the caller.
    """
    response.headers[:] = [
        header for header in response.headers
        if header[0].lower() not in (b"cache-control", b"vary")
    ]
    response.headers.append((b"cache-control", b"private, no-store"))
    response.headers.append((b"vary", b"Authorization, Cookie"))
    return response


def _private_stream(response: SSEResponse) -> SSEResponse:
    """As `_private`, but replacing the `no-cache` `SSEResponse` set.

    Appending a second `cache-control` line would leave the first one --
    `no-cache`, which permits a *shared* store -- as the one a proxy reads.
    """
    response.headers = [
        header for header in response.headers if header[0] != b"cache-control"
    ]
    response.headers.append((b"cache-control", b"private, no-cache, no-store"))
    return response


def _distinct_identifiers(identifiers: list[Any]) -> list[str]:
    """The ids as strings, refusing a list that collapses.

    Ids reach the policy engine as text, so `1` and `"1"` name one entity -- and
    answering for 200 ids with 199 keys, silently, leaves the client to notice a
    row it never got an answer for. Refused instead.
    """
    seen: dict[str, Any] = {}
    for identifier in identifiers:
        text = str(identifier)
        if text in seen:
            raise ValueError(
                f"ids {seen[text]!r} and {identifier!r} are the same identifier once "
                "stringified; send each row once"
            )
        seen[text] = identifier
    return list(seen)


def _principal_key(identity: Any) -> str:
    """The subscription key. Typed, so two kinds of principal cannot collide."""
    return f"{identity.type}::{identity.id}"


async def _decide(
    request: Request,
    authorizer: Any,
    asks: Sequence[tuple[Any, str]],
    *,
    limit: int,
) -> list[bool]:
    """Answer every `(resource, action)` ask, in ask order, `limit` at a time.

    Every ask is one `authorizer.authorize`. With the built-in engine that is
    in-process and the loop never yields, so this is the same work either way --
    the case it exists for is the *remote* authorizer the `CedarEngine`
    protocol invites, where each ask is a round trip. Evaluated one at a time, a
    batch of two hundred ids over three actions is six hundred round trips end
    to end; `limit` at a time it is six hundred divided by `limit`. That is
    a complexity argument, not a measurement -- no benchmark run here can see a
    network that is not in it.

    Bounded rather than gathered whole, because gathering whole would trade this
    endpoint's own denial of service for one aimed at somebody else's
    authorization service: `max_ids` already caps the work at ids x actions,
    and firing all of it at once makes one request a burst of exactly that size.

    `limit <= 1` restores strictly sequential evaluation. It is the escape
    hatch for an authorizer that cannot take concurrent calls carrying one
    request -- see `permissions_router`, which states that requirement.
    """
    if not asks:
        return []
    if limit <= 1 or len(asks) == 1:
        verdicts = []
        for resource, action in asks:
            decision = await authorizer.authorize(
                request, _Requirement(action, resource)
            )
            verdicts.append(bool(getattr(decision, "allowed", False)))
        return verdicts

    gate = asyncio.Semaphore(limit)

    async def one(resource: Any, action: str) -> bool:
        async with gate:
            decision = await authorizer.authorize(
                request, _Requirement(action, resource)
            )
            return bool(getattr(decision, "allowed", False))

    return list(await asyncio.gather(*(one(r, a) for r, a in asks)))


def permission_document(
    app: Any,
    *,
    bus: Any = None,
    roles_model: Any = None,
    channel: str = PERMISSION_CHANNEL,
    keepalive: float = DEFAULT_KEEPALIVE,
    max_subscribers: int = 1024,
    max_per_principal: int = 4,
) -> LiveDocument:
    """The change signal behind `{prefix}/stream`.

    Build one yourself when you need a handle on it -- to call
    `document.notify_all("policies")` from a reload hook, or
    `document.close_all()` at shutdown -- and pass it to
    `permissions_router`. Otherwise that function builds one.

    `bus` (`app.messaging(...)`) is what makes a role change on the worker
    that took the write reach the worker holding the stream. Without it the
    signal is local, which is right for one worker and for tests.

    `roles_model` is your role-membership model, or its name; a committed
    write to it is what makes one user's manifest stale. It has to be named
    because Wreath cannot know which of your tables grants a role -- and without
    it the stream still works, still notices a policy-set change, and still
    delivers an explicit `notify`.
    """
    watch: Iterable[Any] = () if roles_model is None else (roles_model,)
    return LiveDocument(
        channel=channel,
        bus=bus,
        # Read lazily, so this may be built before the routes are declared and
        # so a policy set replaced later is noticed rather than remembered.
        fingerprint=lambda: _shared_fingerprint(app),
        watch=watch,
        watch_reason="roles",
        max_subscribers=max_subscribers,
        max_per_principal=max_per_principal,
        keepalive=keepalive,
    )


def permissions_router(
    app: Any,
    *,
    prefix: str = "/permissions",
    manifest_path: str = "/manifest",
    stream_path: str = "/stream",
    bus: Any = None,
    roles_model: Any = None,
    document: LiveDocument | None = None,
    max_ids: int = DEFAULT_MAX_IDS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> Router:
    """Routes that answer what the caller may do, from the app's own policies.

    `GET  {prefix}`               the vocabulary, so a client can discover it
    `GET  {prefix}/manifest`      what this caller may ever do (`ETag`)
    `GET  {prefix}/stream`        SSE: that manifest moved, ask again
    `POST {prefix}`               what this caller may do to these rows

    Mount it with `app.include_router(permissions_router(app))`. The
    application is passed in because every answer is read off *its* routes and
    evaluated by *its* authorizer -- the endpoint cannot drift from enforcement
    even in principle, and it is only ever read at request time, so the routes
    it describes may be declared after this call.

    **All four require an authenticated caller**, the vocabulary included. It is
    the complete map of the authorization surface -- every resource type and
    every action the application enforces -- and nothing runtime needs it
    anonymously: the generated client bakes the vocabulary in at *build* time,
    from the app object, never over HTTP.

    `max_ids` bounds one batch request. The endpoint runs
    `len(ids) x len(actions)` policy evaluations, so an unbounded list is a
    denial of service one authenticated caller can post. Over the ceiling it
    **refuses**, naming the limit, rather than truncating: a truncated answer
    draws a UI that is confidently wrong, and an absent answer is only absent.

    `max_concurrency` bounds how many of those evaluations are in flight at
    once. It matters only for a **remote** authorizer, which the `CedarEngine`
    protocol invites: the built-in engine is in-process and the loop never
    yields, so concurrency there is the same work in the same order. Evaluating
    a full batch one ask at a time is `max_ids x len(actions)` round trips end
    to end; this makes it that over `max_concurrency`. It is deliberately not
    unbounded -- gathering the whole product would turn one caller's batch into
    a burst of that size against a service that is not yours to overload.

    **An authorizer must therefore tolerate concurrent calls carrying one
    request.** The built-in one does; it reads the request and never writes to
    it. If yours cannot, pass `max_concurrency=1` and evaluation returns to
    strictly sequential.

    `bus` and `roles_model` configure the stream; see
    `permission_document`, which builds the document when you do not pass
    one.
    """
    if document is None:
        document = permission_document(app, bus=bus, roles_model=roles_model)
    live = document  # a non-optional name, so the endpoint's closure has one
    # Read once per route-table change rather than once per request; see
    # `_vocabulary_reader`. Held by the closure, not by the app, so two routers
    # over two apps share nothing.
    vocabulary_now = _vocabulary_reader(app)
    # Absolute paths rather than a router prefix, so the vocabulary lives at
    # exactly `{prefix}` and not `{prefix}/`.
    router = Router()

    @router.get(prefix)
    @add_authenticated
    async def vocabulary(request: Request) -> Any:
        if request.identity is None:
            return _unauthenticated()
        return _private(
            JSONResponse(
                {"resources": {
                    name: list(actions)
                    for name, actions in vocabulary_now().items()
                }}
            )
        )

    @router.get(prefix + manifest_path)
    @add_authenticated
    async def manifest(request: Request) -> Any:
        identity = request.identity
        if identity is None:
            return _unauthenticated()
        authorizer = _authorizer(app)
        if authorizer is None:
            return _unconfigured()
        vocabulary = vocabulary_now()
        # Revalidation is the point: the client fetches this once at sign-in and
        # then sends `If-None-Match` forever. The tag covers everything that can
        # change the answer -- who is asking, what roles they hold, and the
        # policy set itself -- so a promotion or a deploy invalidates it and
        # nothing else does.
        tag = _manifest_etag(
            identity, authorizer, vocabulary, _request_flags(authorizer, request)
        )
        if _etag_matches(request.header("if-none-match"), tag):
            response = Response(b"", status=304)
            response.headers.append((b"etag", tag.encode("ascii")))
            return _private(response)

        # The resource is the *type*, not a row: this answers "could this
        # principal ever" so a nav item can be drawn. Row-level questions go to
        # the batch endpoint, which is why both exist.
        #
        # Flattened across resource types before evaluating, so `max_concurrency`
        # bounds the whole manifest rather than each type in turn -- a type with
        # one action would otherwise pace the type with ten.
        asks = [
            (_entity(resource_type, TYPE_LEVEL_ID), action)
            for resource_type, actions in vocabulary.items()
            for action in actions
        ]
        verdicts = await _decide(request, authorizer, asks, limit=max_concurrency)
        allowed: dict[str, list[str]] = {}
        at = 0
        for resource_type, actions in vocabulary.items():
            granted = [
                action
                for offset, action in enumerate(actions)
                if verdicts[at + offset]
            ]
            at += len(actions)
            if granted:
                allowed[resource_type] = granted
        body = JSONResponse({
            "principal": identity.id,
            "roles": sorted(identity.roles),
            "allowed": allowed,
        })
        body.headers.append((b"etag", tag.encode("ascii")))
        return _private(body)

    @router.get(prefix + stream_path)
    @add_authenticated
    async def stream(request: Request) -> Any:
        identity = request.identity
        if identity is None:
            return _unauthenticated()
        authorizer = _authorizer(app)
        if authorizer is None:
            return _unconfigured()
        # Everything this stream needs is already in hand: the identity from
        # authentication and the authorizer from the app. Nothing below touches
        # the database, which matters because this response stays open for as
        # long as the tab does -- a stream holding a connection would be a pool
        # exhausted by idle browsers.
        subscription = live.subscribe(_principal_key(identity))
        if subscription is None:
            # Refused rather than queued: the registry is bounded on purpose. A
            # caller without a stream is not broken -- it revalidates the
            # manifest with `If-None-Match`, which is this feature minus the
            # push -- so degrading to that is better than a slot nobody frees.
            return _private(
                ProblemResponse(
                    status=503,
                    detail="too many open permission streams; poll the manifest",
                )
            )

        def tag_for(reason: str) -> str | None:
            """The new tag, but only when it can be stated truthfully.

            The manifest's tag covers the caller's roles, and `identity` was
            captured when the stream opened. On a *policy* change those roles
            are still current, so the recomputed tag is the real one and a
            client holding it can skip the refetch. On a *roles* change the
            identity in hand is stale by definition -- computing a tag from it
            would tell the client to skip exactly the refetch it needs, so this
            says nothing and lets a conditional request settle it.
            """
            if reason != "policies":
                return None
            # The flags are this stream's, resolved when it opened, and a flip
            # is not a "policies" reason so it never reaches here on its own.
            # On a genuine policy change they are as stale as `identity` is,
            # and the paragraph above is the standing answer for that class:
            # what cannot be stated truthfully is settled by a conditional
            # request rather than guessed at.
            return _manifest_etag(
                identity, authorizer, vocabulary_now(), _request_flags(authorizer, request)
            )

        return _private_stream(change_stream(subscription, tag_for=tag_for))

    @router.post(prefix)
    @add_authenticated
    async def batch(request: Request) -> Any:
        if request.identity is None:
            return _unauthenticated()
        authorizer = _authorizer(app)
        if authorizer is None:
            return _unconfigured()
        payload = await request.json()
        if not isinstance(payload, dict):
            return _bad("the body must be a JSON object")
        resource_type = payload.get("type")
        identifiers = payload.get("ids")
        if not isinstance(resource_type, str) or not isinstance(identifiers, list):
            return _bad("`type` (string) and `ids` (list) are required")
        # Before anything is evaluated: the work below is `ids x actions` policy
        # decisions on one connection, so the list length is the whole cost.
        # Refused, not truncated -- a short answer would draw a table whose
        # remaining rows are silently unauthorized, which is worse than a page
        # that says it asked for too much.
        if len(identifiers) > max_ids:
            return _bad(
                f"at most {max_ids} ids per request; {len(identifiers)} were sent"
            )

        vocabulary = vocabulary_now()
        known = vocabulary.get(resource_type)
        if known is None:
            return _bad(f"no policies are declared for {resource_type!r}")
        actions = payload.get("actions")
        if actions is None:
            actions = known
        elif not isinstance(actions, list) or not all(
            isinstance(item, str) for item in actions
        ):
            return _bad("`actions` must be a list of strings")
        else:
            # Refused rather than evaluated: an endpoint that answers for any
            # action a caller invents is an oracle for probing the policy set.
            unknown = [action for action in actions if action not in known]
            if unknown:
                return _bad(
                    f"undeclared action(s) for {resource_type}: {', '.join(unknown)}"
                )
            # Deduplicated, order preserved. `max_ids` bounds one side of the
            # ids x actions product and nothing bounded the other, so a repeated
            # action was a way to multiply the work past the ceiling this
            # endpoint refuses ids over.
            actions = tuple(dict.fromkeys(str(action) for action in actions))

        try:
            texts = _distinct_identifiers(identifiers)
        except ValueError as error:
            return _bad(str(error))
        # Flattened across ids as well as actions: ids is the axis that grows
        # (up to `max_ids`), so bounding per id would leave the request as
        # `len(ids)` sequential rounds however small the concurrency limit.
        chosen = tuple(actions)
        asks = [
            (_entity(resource_type, text), action)
            for text in texts
            for action in chosen
        ]
        verdicts = await _decide(request, authorizer, asks, limit=max_concurrency)
        permissions = {}
        for index, text in enumerate(texts):
            base = index * len(chosen)
            permissions[text] = [
                action
                for offset, action in enumerate(chosen)
                if verdicts[base + offset]
            ]
        return _private(
            JSONResponse({"type": resource_type, "permissions": permissions})
        )

    return router


def _authorizer(app: Any) -> Any:
    return getattr(app, "_authorizer", None)


def _unauthenticated() -> Response:
    return ProblemResponse(
        status=401, detail="permissions are per-principal; authenticate first"
    )


def _unconfigured() -> Response:
    return ProblemResponse(
        status=500,
        detail="no authorization provider is configured; see app.configure_auth",
    )


def _bad(detail: str) -> Response:
    return ProblemResponse(status=400, detail=detail)


def _manifest_etag(
    identity: Any,
    authorizer: Any,
    vocabulary: dict[str, tuple[str, ...]],
    flags: frozenset[str] = frozenset(),
) -> str:
    """A tag over every input that can change the answer.

    The policy set is fingerprinted through the authorizer so a deploy that
    widens a rule invalidates every cached manifest; the roles are included so a
    promotion invalidates one; and the caller's enabled feature flags are
    included because a policy may read `context.flags`, so a flip changes the
    answer exactly as a promotion does. Without them a client would hold a
    manifest drawn for the old flag state and keep revalidating it successfully
    -- chrome for something now denied, with no event that could correct it.

    Nothing else can move the answer, which is why a client can hold the
    manifest until this changes.
    """
    digest = hashlib.blake2s(digest_size=16)
    digest.update(f"{identity.type}::{identity.id}\x00".encode())
    for role in sorted(identity.roles):
        digest.update(f"{role}\x00".encode())
    digest.update(b"\x01")
    for resource_type, actions in vocabulary.items():
        digest.update(f"{resource_type}\x00{'\x00'.join(actions)}\x00".encode())
    digest.update(b"\x02")
    digest.update(_policy_fingerprint(authorizer))
    digest.update(b"\x03")
    for flag in sorted(flags):
        digest.update(f"{flag}\x00".encode())
    return f'W/"{digest.hexdigest()}"'


def _request_flags(authorizer: Any, request: Any) -> frozenset[str]:
    """The caller's enabled flags, when the authorizer can say.

    Optional capability, probed like `fingerprint`/`source`/`policies`: an
    authorizer that does not model flags contributes nothing to the tag, which
    is the same tag it produced before flags existed.
    """
    resolve = getattr(authorizer, "flags_for", None)
    if not callable(resolve):
        return frozenset()
    return cast("frozenset[str]", resolve(request))


def _shared_fingerprint(app: Any) -> str:
    """Everything in `_manifest_etag` except *who is asking*.

    An open stream re-reads this on each keep-alive tick, so a policy set
    replaced in-process moves it and every stream says "refetch" without a
    reload hook having to remember to. It is deliberately the shared half only:
    the per-principal half cannot be compared across subscribers, and this is
    computed once per worker rather than once per stream.

    `""` when there is no authorizer -- nothing to drift from.
    """
    authorizer = _authorizer(app)
    if authorizer is None:
        return ""
    digest = hashlib.blake2s(digest_size=16)
    for resource_type, actions in declared_actions(app).items():
        digest.update(f"{resource_type}\x00{'\x00'.join(actions)}\x00".encode())
    digest.update(b"\x02")
    digest.update(_policy_fingerprint(authorizer))
    return digest.hexdigest()


#: A random tag per engine *instance*, for engines that expose nothing to hash.
#: Weak, so an engine dropped by a reload takes its token with it rather than
#: holding a whole policy set for the life of the process.
_INSTANCE_TOKENS: WeakKeyDictionary[Any, bytes] = WeakKeyDictionary()

#: The same, for an engine that can be neither weak-referenced (a `__slots__`
#: class without `__weakref__`) nor hashed. Keyed by address and holding the
#: engine, which is the whole point: a retained engine's address cannot be
#: handed to anything else, so the key stays unique. It does retain one engine
#: per such instance -- accepted, because the alternative is a tag that changes
#: on every read, and `_shared_fingerprint` is re-read on every stream
#: keep-alive tick; that would tell every open stream the policies moved, every
#: few seconds, forever.
#:
#: **Bounded**, because "one engine per instance, forever" is a leak in any
#: process that builds engines repeatedly -- a reload loop, a test suite. An
#: evicted engine simply gets a fresh token next time, which reads as "the
#: policy set changed" and costs one refetch.
_PINNED_TOKENS: BoundedCache = BoundedCache(max_entries=64)


def _instance_token(engine: Any) -> bytes:
    """A random tag minted once for this engine object.

    Deliberately **not** `id(engine)`. CPython reuses addresses aggressively
    -- freeing one `__slots__` instance and allocating the next of the same
    shape lands on the same address reproducibly -- so a reload could replace
    the engine without moving the `ETag`, and every client holding a manifest
    would keep serving a stale one with no event that could ever correct it. A
    random token cannot collide that way. It also keeps a heap address out of a
    client-visible header.
    """
    try:
        token = _INSTANCE_TOKENS.get(engine)
        if token is None:
            token = token_bytes(16)
            _INSTANCE_TOKENS[engine] = token
        return token
    except TypeError:
        pinned = _PINNED_TOKENS.get(id(engine))
        if pinned is None:
            pinned = (engine, token_bytes(16))
            _PINNED_TOKENS.set(id(engine), pinned)
        return pinned[1]


def _policy_fingerprint(authorizer: Any) -> bytes:
    """Identify the policy set behind `authorizer`, however it is shaped.

    Content first, because a content-derived tag is the same on every worker
    and across a restart -- which is what lets a client hold its manifest
    through a rolling deploy that did not touch the policies. An authorizer
    that exposes nothing gets `_instance_token` instead, which cannot say
    *which* policy set this is but does reliably say *a different one*.
    """
    # Opportunistic hooks, asked of the authorizer itself: neither the
    # `Authorizer` nor the `CedarEngine` protocol requires any of them.
    # `CedarAuthorizer` delegates all three to its engine, and the built-in
    # `CedarPolicies` answers `source`, which is why the shipped configuration
    # gets a content-derived tag rather than a per-instance one and keeps
    # cross-worker revalidation. Asking the authorizer rather than digging out
    # its engine is what keeps the private name in the file that owns it.
    for attribute in ("fingerprint", "source", "policies"):
        value = getattr(authorizer, attribute, None)
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
    # The reach survives here alone, and only to keep the token *per engine*:
    # two authorizers over one policy set must agree, or a second adapter over
    # the same engine would mint a second tag and every client would refetch a
    # manifest that had not moved. The asymmetry is the point -- on this path a
    # renamed `_engine` costs a redundant refetch, where on the content path
    # above it silently served a stale manifest forever. `tests/test_permissions.py`
    # pins the name so the rename fails loudly instead.
    return _instance_token(getattr(authorizer, "_engine", authorizer))
