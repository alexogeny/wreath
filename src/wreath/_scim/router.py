"""The SCIM 2.0 endpoints -- RFC 7644 -- over `wreath.organizations`.

`wreath.organizations.scim_router` is the public name; everything here is what
it builds. The routes are ordinary wreath routes: they authenticate through the
application's own backend and authorize through its own `CedarAuthorizer`, so
there is no allow/deny decision in this module at all. Every handler carries
`@authorize`, and a deployment with no authorizer configured is refused when the
router is built rather than serving a provisioning API to anyone with a session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace as _replace
from typing import Any

from .._codecs import parse_qs
from .._userkit import hash_password
from ..authorization import authorize
from ..cache import BoundedCache
from ..response import JSONResponse, Response
from ..router import Router
from . import resources
from .filters import FilterError, select
from .filters import parse as parse_filter
from .patch import PatchError
from .patch import apply as apply_patch
from .patch import replace as replace_document

__all__ = ["ScimResponse", "scim_router"]

#: A password hash no password can produce. `wreath.users` stores
#: `scrypt$n$r$p$salt$hash`; this has no `$` at all, so `verify_password` fails
#: to unpack it, catches its own `ValueError` and answers `False` -- for every
#: input, without spending scrypt on it. A user a directory provisioned has no
#: password until one is set, and the fail-closed spelling of "no password" is a
#: hash that cannot be produced rather than an empty string that might be.
UNUSABLE_PASSWORD = "!scim-provisioned"

#: Largest page a client may ask for, and the default when it asks for none.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 100

#: How many of an organisation's members a *filtered* list may examine. A filter
#: is evaluated against each member's SCIM representation, and building one
#: reads that user from the user store, so an unbounded organisation is an
#: unbounded fan-out one request long. Over the ceiling the endpoint refuses
#: with the `tooMany` of section 3.12 rather than answering from a prefix --
#: a truncated page reads as "these are all the matches" and is how a directory
#: decides to recreate everyone it could not see.
MAX_FILTER_SCAN = 1000

# Client filter text is bounded to 2 KiB by the parser, and these router-owned
# per-resource tables bound how many successful immutable parses survive. A
# separate table per attribute vocabulary lets the client text itself be the
# lookup key. Invalid filters are deliberately not cached, so they still take
# the parser's exact refusal path on every request.
_FILTER_CACHE_SIZE = 64


class ScimResponse(JSONResponse):
    """A JSON body sent as `application/scim+json`, which RFC 7644 section 3.1 requires."""

    media_type = b"application/scim+json"


def _error(status: int, detail: str, scim_type: str | None = None) -> ScimResponse:
    return ScimResponse(resources.error_document(status, detail, scim_type), status=status)


def _refused(error: PatchError) -> ScimResponse:
    """A `PatchError` as its HTTP answer.

    One mapping for every write route. A `uniqueness` refusal is the only one
    that is not a 400 -- section 3.3 gives it 409 -- and writing that choice
    once means `POST`, `PUT` and `PATCH` cannot answer the same refusal
    differently.
    """
    return _error(409 if error.scim_type == "uniqueness" else 400, error.detail, error.scim_type)


def _entity_safe(organization: str) -> str:
    """`organization` if it can be spelled inside a Cedar entity reference.

    `Organization::"<id>"` is a quoted string, so an id carrying a quote or a
    backslash would end the literal early and change which entity the policy is
    asked about. `wreath.organizations.Organization` already refuses a colon for
    the neighbouring reason; this refuses the two characters that matter here.
    """
    if not organization:
        raise ValueError("a SCIM router needs a non-empty organization id")
    if '"' in organization or "\\" in organization:
        raise ValueError(
            f"an organization id used by scim_router must not contain a quote or "
            f"a backslash (got {organization!r}); it is spelled into the Cedar "
            'entity reference Organization::"<id>"'
        )
    return organization


def _constant(value: str) -> Callable[[Any], str]:
    """A one-organisation resolver, so the per-request path has no second shape."""

    def read(_request: Any) -> str:
        return value

    return read


def _require(store: Any, methods: Iterable[str], what: str) -> None:
    missing = [name for name in methods if not callable(getattr(store, name, None))]
    if missing:
        raise ValueError(
            f"scim_router needs a {what} offering {', '.join(missing)}; "
            f"{type(store).__name__} does not. See the guide for the two seams "
            "SCIM provisions through."
        )


def _parsed_filter(
    cache: BoundedCache,
    expression: str,
    attributes: frozenset[str],
) -> Any:
    """Reuse one successful parse within the router that owns ``cache``."""
    node = cache.get(expression)
    if node is None:
        node = parse_filter(expression, attributes=attributes)
        cache.set(expression, node)
    return node


def _sortable_value(value: Any) -> tuple[bool, str]:
    if isinstance(value, list):
        primary = next(
            (item for item in value if isinstance(item, Mapping) and item.get("primary") is True),
            value[0] if value else None,
        )
        value = primary.get("value") if isinstance(primary, Mapping) else primary
    if value is None:
        return True, ""
    if isinstance(value, bool):
        return False, "1" if value else "0"
    return False, str(value).casefold()


def scim_router(
    app: Any,
    *,
    users: Any,
    organizations: Any,
    organization: str | Callable[[Any], str],
    prefix: str = "/scim/v2",
    read_action: str = "scim_read",
    write_action: str = "scim_write",
    page_size: int = DEFAULT_PAGE_SIZE,
    max_page_size: int = MAX_PAGE_SIZE,
    max_filter_scan: int = MAX_FILTER_SCAN,
) -> Router:
    """SCIM 2.0 provisioning endpoints for one organisation, over stores you already have.

    ```python
    app.configure_auth(backend, CedarAuthorizer(engine=CedarPolicies(POLICY),
                                                organizations=Memberships(orgs)))
    app.include_router(scim_router(app, users=users, organizations=orgs,
                                   organization="acme"))
    ```

    | Route | What it does |
    | --- | --- |
    | `GET {prefix}/ServiceProviderConfig` | what this provider supports |
    | `GET {prefix}/ResourceTypes[/{name}]` | `User` and `Group` |
    | `GET {prefix}/Schemas[/{urn}]` | the attributes actually implemented |
    | `GET/POST {prefix}/Users` | list with `filter`, or provision one |
    | `GET/PUT/PATCH/DELETE {prefix}/Users/{id}` | one member |
    | `GET {prefix}/Groups[/{id}]` | the organisation's roles |
    | `PUT/PATCH {prefix}/Groups/{id}` | who holds a role |

    **SCIM keeps no store of its own.** A `User` is a record in `users`, a
    `Group` is a role in `organizations.roles()`, and membership is a
    `Membership` in the organisation. Provisioning writes what any other part of
    the application would write, which is what makes a de-provisioned user
    actually lose access: `context.organizations` and `context.org_roles` read
    the same rows.

    **There is no authorization decision in this module.** Every route carries
    `@authorize(action=..., resource='Organization::"<org>"')` and the
    application's own `CedarAuthorizer` answers it, so a SCIM client is bounded
    by the same policy set as everything else and appears in
    `permissions_router`'s vocabulary. A policy has to permit it:

        permit (principal in Group::"directory", action == Action::"scim_write",
                resource == Organization::"acme");

    Building the router **refuses** when the application has no authorizer
    configured, rather than serving a provisioning API to any authenticated
    caller -- the refuse-rather-than-half-wire rule in `AGENTS.md` is why that
    is the right way round.

    ## The two de-provisioning verbs, which are not the same

    * `DELETE /Users/{id}` removes the **membership**. The account survives, and
      so does everything it owns -- de-provisioning a directory user must not
      delete their content, and a cascade here would make an accidental one
      unrecoverable. The resource then answers 404, which is what a directory
      expects of a user it removed.
    * `PATCH` or `PUT` setting `active: false` disables the **account**, which
      is `UserRecord.is_active` and is not scoped to an organisation. When the
      user is a member of another organisation as well, that write is
      **refused** with `scimType: mutability` naming the count: one tenant's
      directory does not get to sign a user out of another tenant. `DELETE` is
      the scoped verb and the refusal says so.

    `POST /Users` with a `userName` that already has an account **adopts** it
    into the organisation and answers 201; the same call for someone already
    provisioned here answers 409 `uniqueness`. A directory that retries a call
    it already made therefore converges rather than duplicating.

    ## What it does not implement, and why the refusals are loud

    Bulk operations and `ETag` are absent, and `ServiceProviderConfig` says so.
    Lists support bounded `sortBy`/`sortOrder`. `POST` and `DELETE` on `/Groups`
    answer 501: a group here is a role
    in the declared vocabulary, which is configuration a policy names by string,
    not a row a client may mint. A filter naming an attribute this provider does
    not hold -- `externalId`, `name.givenName`, anything else section 4.1 makes
    optional -- is a 400 rather than an empty page, because an empty page is
    what makes a directory decide the user is missing and create them twice.

    Args:
        app: the application these routes will be mounted on. Read for its
            authorizer and its authentication backend; the routes are its own.
        users: a `wreath.users` `UserStore` -- `get_by_id`, `get_by_email`,
            `create`, `update`.
        organizations: an `OrganizationStore` that also offers
            `members(org_id)`, which the authorization path never needs and a
            directory listing always does.
        organization: which organisation this router provisions into, or a
            `(request) -> str` reading it from a path parameter or a subdomain.
            The same value becomes the Cedar resource, so the data scope and the
            policy decision cannot disagree about which tenant is meant.
        prefix: where the routes mount. `{name}` placeholders are filled from
            the request's path parameters when building `meta.location`.
        read_action: the Cedar action name the read routes ask about.
        write_action: the Cedar action name the write routes ask about.
        page_size: `count` when a client sends none.
        max_page_size: the largest `count` honoured.
        max_filter_scan: how many members a *filtered* list may examine before
            refusing with `tooMany`. A filter is evaluated per member and
            building one member's representation reads their account, so this
            bounds the fan-out one request can ask for.

    Returns:
        A `Router` to pass to `app.include_router`.

    Raises:
        ValueError: a store missing a method, an application with no authorizer,
            an empty action name, an inconsistent page size, or an organisation
            id that cannot be spelled inside a Cedar entity reference.
    """
    _require(users, ("get_by_id", "get_by_email", "create", "update"), "user store")
    _require(
        organizations,
        ("members", "memberships", "roles", "add_member", "remove_member"),
        "organization store",
    )
    if getattr(app, "_authorizer", None) is None:
        raise ValueError(
            "scim_router refuses to build without an authorizer: every route it "
            "declares is gated by a Cedar policy, and an application that never "
            "called configure_auth(backend, CedarAuthorizer(...)) would answer "
            "every provisioning request with a 500 at best and expose the "
            "directory to any authenticated caller at worst"
        )
    if not read_action or not write_action:
        raise ValueError("scim_router needs a non-empty read_action and write_action")
    # `max_page_size < 1` needs no clause of its own: `page_size` is at least
    # one by the first test, so a ceiling below one always fails the third.
    if page_size < 1 or page_size > max_page_size:
        raise ValueError(
            f"scim_router needs 1 <= page_size <= max_page_size (got "
            f"page_size={page_size}, max_page_size={max_page_size})"
        )
    if max_filter_scan < 1:
        raise ValueError("scim_router needs a positive max_filter_scan")
    resolve: Callable[[Any], str]
    if isinstance(organization, str):
        resolve = _constant(_entity_safe(organization))
    else:
        resolve = organization

    def org_of(request: Any) -> str:
        """Which organisation this request provisions into.

        Re-checked per request rather than only at build time, because a
        callable reads it from the request -- a path parameter, a subdomain --
        and that value reaches a Cedar entity reference.
        """
        return _entity_safe(str(resolve(request)))

    def cedar_resource(request: Any) -> str:
        return f'Organization::"{org_of(request)}"'

    def base_of(request: Any) -> str:
        """The absolute URL these resources live under, for `meta.location`.

        Built from the request's own scheme and `Host`, which is what a
        directory will call back on. Behind a TLS-terminating proxy the scheme
        is the one this process accepted -- add `ProxyPolicy` for the
        forwarded one, exactly as every other absolute URL wreath builds needs.
        """
        mounted = prefix.format(**request.path_params) if "{" in prefix else prefix
        root = str(request.scope.get("root_path", "")).rstrip("/")
        host = request.header("host")
        if not host:
            return f"{root}{mounted}"
        return f"{request.scheme}://{host}{root}{mounted}"

    router = Router(prefix=prefix, tags=("scim",))
    user_filter_cache = BoundedCache(max_entries=_FILTER_CACHE_SIZE)
    group_filter_cache = BoundedCache(max_entries=_FILTER_CACHE_SIZE)

    async def membership_map(org: str) -> dict[str, frozenset[str]]:
        """Every member of `org`, user id -> roles, one store call."""
        return {
            membership.user_id: membership.roles for membership in await organizations.members(org)
        }

    async def roles_in(org: str, user_id: str) -> frozenset[str] | None:
        """This user's roles in `org`, or `None` when they are not a member."""
        for membership in await organizations.memberships(user_id):
            if membership.organization == org:
                return membership.roles
        return None

    async def user_and_roles(org: str, user_id: str) -> tuple[Any, frozenset[str]] | None:
        """The record and roles behind `/Users/{id}`, or `None` for a non-member.

        A user who exists but holds no membership here is **not found**, not
        forbidden: the directory provisioning `acme` may not learn that an
        account exists in `globex` by asking for its id.
        """
        held = await roles_in(org, user_id)
        if held is None:
            return None
        record = await users.get_by_id(user_id)
        if record is None:
            return None
        return record, held

    async def find_by_user_name(name: str) -> Any:
        """The account carrying `name`, or `None`.

        `get_by_email` alone is not that question. A store's address index may
        answer for an address a record no longer carries -- `InMemoryUserStore`
        documents exactly this after a rename -- and a stale hit would make a
        `POST` adopt somebody else's account and a rename collide with an
        address nobody holds. The rule is one line and lives in one place: an
        address resolves to a user only while that user still carries it.
        """
        found = await users.get_by_email(name)
        if found is None or found.email.strip().lower() != name.strip().lower():
            return None
        return found

    async def hashed(password: str) -> str:
        """`password` hashed off the event loop, as `wreath.users` does it."""
        return await asyncio.to_thread(hash_password, password)

    async def commit_user(
        org: str, record: Any, current: Mapping[str, Any], target: Mapping[str, Any]
    ) -> Any:
        """Reconcile a user document against the store, or raise `PatchError`.

        The one write path. `PUT`, `PATCH` and the create-time body all reach
        the store through here, so none of them can invent a different meaning
        for `userName` or `active`.
        """
        updated = record
        wanted_name = target.get("userName")
        if not isinstance(wanted_name, str) or not wanted_name.strip():
            raise PatchError("invalidValue", "userName must be a non-empty string")
        wanted_name = wanted_name.strip()
        if wanted_name.lower() != str(current.get("userName", "")).lower():
            clash = await find_by_user_name(wanted_name)
            # No `clash.id != record.id` clause: `current` was serialized from
            # `record`, so reaching this branch already means the wanted name is
            # not this record's, and the second spelling of that could only
            # drift away from the first.
            if clash is not None:
                raise PatchError(
                    "uniqueness",
                    f"another user already has the userName {wanted_name!r}",
                )
            updated = _replace(updated, email=wanted_name)
        wanted_active = target.get("active", current.get("active"))
        if not isinstance(wanted_active, bool):
            raise PatchError("invalidValue", "active must be true or false")
        if wanted_active != bool(current.get("active")):
            elsewhere = [
                membership
                for membership in await organizations.memberships(record.id)
                if membership.organization != org
            ]
            if elsewhere:
                # `UserRecord.is_active` is the account, not the membership, and
                # this account is shared. Disabling it here would sign the user
                # out of somebody else's tenant, and enabling it would undo that
                # tenant's own decision. DELETE is the scoped de-provisioning
                # and the detail says so.
                raise PatchError(
                    "mutability",
                    f"this account is also a member of {len(elsewhere)} other "
                    "organization(s), and 'active' is a property of the account "
                    "rather than of one membership; DELETE this resource to "
                    "de-provision it from this organization alone",
                )
            updated = _replace(updated, is_active=wanted_active)
        password = target.get("password")
        if password is not None:
            if not isinstance(password, str) or not password:
                raise PatchError("invalidValue", "password must be a non-empty string")
            updated = _replace(updated, hashed_password=await hashed(password))
        if updated is record:
            return record
        return await users.update(updated)

    async def commit_group(org: str, role: str, target: Mapping[str, Any]) -> None:
        """Reconcile a group document's membership against the store."""
        wanted: list[str] = []
        # `target["members"]` is a list of mappings by construction: it comes
        # from `group_document`, and both `apply` and `replace` normalise a
        # multi-valued attribute through `_elements` before it lands. Defending
        # against the other shapes here would be defending against this
        # module's own output.
        for element in target["members"]:
            value = element.get("value")
            if not isinstance(value, str) or not value:
                raise PatchError("invalidValue", "every group member needs a string 'value'")
            wanted.append(value)
        held = await membership_map(org)
        for user_id in wanted:
            if user_id not in held:
                raise PatchError(
                    "invalidValue",
                    f"user {user_id!r} is not a member of this organization, so "
                    "they cannot be given a role in it; provision the user first",
                )
        wanted_ids = set(wanted)
        for user_id, roles in held.items():
            should_hold = user_id in wanted_ids
            if should_hold == (role in roles):
                continue
            next_roles = (roles | {role}) if should_hold else (roles - {role})
            await organizations.add_member(org, user_id, roles=next_roles)

    def query_of(request: Any) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, value in parse_qs(request.query_string):
            values.setdefault(key.lower(), value)
        return values

    def sort_documents(
        documents: list[dict[str, Any]],
        query: Mapping[str, str],
        shape: resources.Shape,
    ) -> list[dict[str, Any]]:
        """Apply SCIM section 3.4.2.3 sorting before paging.

        Only stored/published top-level attributes are accepted. Missing values
        sort after present ones in both directions; a multi-valued attribute uses
        its primary value, or first value when none is primary.
        """
        requested = query.get("sortby")
        if requested is None:
            return documents
        key = requested.lower()
        if key not in shape.queryable:
            raise PatchError(
                "invalidValue",
                f"sortBy names unsupported attribute {requested!r}; expected one of "
                + ", ".join(sorted(shape.canonical[name] for name in shape.queryable)),
            )
        order = query.get("sortorder", "ascending").lower()
        if order not in {"ascending", "descending"}:
            raise PatchError(
                "invalidValue",
                f"sortOrder must be 'ascending' or 'descending' (got {order!r})",
            )
        wire_name = shape.canonical[key]

        def sortable(document: dict[str, Any]) -> tuple[bool, str]:
            return _sortable_value(document.get(wire_name))

        present = [document for document in documents if not sortable(document)[0]]
        missing = [document for document in documents if sortable(document)[0]]
        present.sort(key=lambda document: sortable(document)[1], reverse=order == "descending")
        return present + missing

    def paging(query: Mapping[str, str]) -> tuple[int, int]:
        """`(startIndex, count)`, per section 3.4.2.4, or raise `PatchError`.

        Both are 1-based and both are clamped rather than rejected where the
        specification says to: a negative `count` "SHALL be interpreted as 0",
        and a `startIndex` below 1 "SHALL be interpreted as 1". A value that is
        not a number at all is a client bug rather than an out-of-range
        request, and is refused.
        """
        start = 1
        count = page_size
        if "startindex" in query:
            start = max(1, _integer(query["startindex"], "startIndex"))
        if "count" in query:
            count = min(max_page_size, max(0, _integer(query["count"], "count")))
        return start, count

    def _integer(raw: str, name: str) -> int:
        try:
            return int(raw)
        except ValueError:
            raise PatchError("invalidValue", f"{name} must be an integer (got {raw!r})") from None

    def selected(
        documents: list[dict[str, Any]], query: Mapping[str, str], shape: resources.Shape
    ) -> ScimResponse:
        """Filter, page and envelope a list of already-built representations."""
        expression = query.get("filter")
        if expression:
            cache = user_filter_cache if shape is resources.USER else group_filter_cache
            node = _parsed_filter(cache, expression, shape.queryable)
            documents = select(node, documents)
        documents = sort_documents(documents, query, shape)
        start, count = paging(query)
        window = documents[start - 1 : start - 1 + count]
        return ScimResponse(
            resources.list_response(
                window, total=len(documents), start_index=start, per_page=len(window)
            )
        )

    def authentication_scheme(request: Any) -> dict[str, Any]:
        """What section 5 calls `authenticationSchemes`, read off the app's backend.

        Reported rather than configured, because the application already decided
        it: the backend's `WWW-Authenticate` challenge names the scheme a client
        must use to reach these very routes, and a hand-set value here could
        contradict the one that is actually enforced.
        """
        backend = getattr(app, "_auth_backend", None)
        offer = getattr(backend, "challenge", None)
        challenge = offer(request) if callable(offer) else None
        name = (challenge if isinstance(challenge, str) else "Bearer").split(" ")[0]
        if name.lower() == "basic":
            return {
                "type": "httpbasic",
                "name": "HTTP Basic",
                "description": "Authentication with an HTTP Basic credential",
                "specUri": "https://www.rfc-editor.org/info/rfc7617",
                "primary": True,
            }
        return {
            "type": "oauthbearertoken",
            "name": "OAuth Bearer Token",
            "description": "Authentication with an OAuth 2.0 bearer token",
            "specUri": "https://www.rfc-editor.org/info/rfc6750",
            "primary": True,
        }

    @router.get("/ServiceProviderConfig")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_service_provider_config(request: Any) -> Response:
        return ScimResponse(
            resources.service_provider_config(
                base=base_of(request),
                max_results=max_filter_scan,
                scheme=authentication_scheme(request),
            )
        )

    @router.get("/ResourceTypes")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_resource_types(request: Any) -> Response:
        found = resources.resource_types(base=base_of(request))
        return ScimResponse(
            resources.list_response(found, total=len(found), start_index=1, per_page=len(found))
        )

    @router.get("/ResourceTypes/{name}")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_resource_type(request: Any) -> Response:
        wanted = request.path_params["name"]
        for found in resources.resource_types(base=base_of(request)):
            if found["id"] == wanted:
                return ScimResponse(found)
        return _error(404, f"no resource type named {wanted!r}")

    @router.get("/Schemas")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_schemas(request: Any) -> Response:
        found = resources.schema_documents(base=base_of(request))
        return ScimResponse(
            resources.list_response(found, total=len(found), start_index=1, per_page=len(found))
        )

    @router.get("/Schemas/{urn}")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_schema(request: Any) -> Response:
        wanted = request.path_params["urn"]
        for found in resources.schema_documents(base=base_of(request)):
            if found["id"] == wanted:
                return ScimResponse(found)
        return _error(404, f"no schema named {wanted!r}")

    async def user_documents(org: str, base: str, limit: int) -> list[dict[str, Any]]:
        """Every member's representation, refusing an organisation over `limit`."""
        held = await membership_map(org)
        if len(held) > limit:
            raise PatchError(
                "tooMany",
                f"this organization has {len(held)} members and this provider "
                f"evaluates a filter over at most {limit}; narrow the request or "
                "raise scim_router(max_filter_scan=...)",
            )
        found: list[dict[str, Any]] = []
        for user_id in sorted(held):
            record = await users.get_by_id(user_id)
            if record is not None:
                found.append(resources.user_document(record, roles=held[user_id], base=base))
        return found

    @router.get("/Users")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_list_users(request: Any) -> Response:
        org = org_of(request)
        base = base_of(request)
        query = query_of(request)
        try:
            if query.get("filter") or query.get("sortby"):
                return selected(
                    await user_documents(org, base, max_filter_scan),
                    query,
                    resources.USER,
                )
            # Unfiltered: page over the membership list and read only the users
            # on the page, so an organisation larger than one page costs one
            # page of lookups rather than all of them.
            held = await membership_map(org)
            start, count = paging(query)
            ordered = sorted(held)
            found = []
            for user_id in ordered[start - 1 : start - 1 + count]:
                record = await users.get_by_id(user_id)
                if record is not None:
                    found.append(resources.user_document(record, roles=held[user_id], base=base))
            return ScimResponse(
                resources.list_response(
                    found, total=len(ordered), start_index=start, per_page=len(found)
                )
            )
        except FilterError as error:
            return _error(400, error.detail, "invalidFilter")
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)

    @router.get("/Users/{id}")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_get_user(request: Any) -> Response:
        org = org_of(request)
        found = await user_and_roles(org, request.path_params["id"])
        if found is None:
            return _error(404, "no such user in this organization")
        record, held = found
        return ScimResponse(resources.user_document(record, roles=held, base=base_of(request)))

    @router.post("/Users")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_create_user(request: Any) -> Response:
        org = org_of(request)
        base = base_of(request)
        body = await _json(request)
        if body is None:
            return _error(400, "request body is not valid JSON", "invalidSyntax")
        if not isinstance(body, Mapping):
            return _error(400, "request body must be a JSON object", "invalidSyntax")
        name = body.get("userName")
        if not isinstance(name, str) or not name.strip():
            return _error(400, "userName is required", "invalidValue")
        name = name.strip()
        record = await find_by_user_name(name)
        if record is not None and await roles_in(org, record.id) is not None:
            return _error(
                409,
                f"a user with the userName {name!r} is already provisioned in this organization",
                "uniqueness",
            )
        if record is None:
            record = await users.create(name, UNUSABLE_PASSWORD)
        current = resources.user_document(record, roles=(), base=base)
        try:
            # The membership is written **last**, after every refusal this body
            # can draw, so a rejected create leaves the organisation exactly as
            # it was. (The account may already have been created by then and is
            # left in place: the next attempt adopts it, where deleting it would
            # delete an account another organisation may meanwhile have
            # adopted.) Nothing above depends on the membership existing --
            # `commit_user` counts the *other* organisations either way.
            target = replace_document(current, body, shape=resources.USER)
            record = await commit_user(org, record, current, target)
            # An account that already exists is **adopted** rather than
            # duplicated: the same person may have signed up directly before the
            # directory ever provisioned them, and minting a second account for
            # one email is how a user ends up locked out of the one holding
            # their data.
            await organizations.add_member(org, record.id, roles=())
        except PatchError as error:
            return _refused(error)
        document = resources.user_document(record, roles=(), base=base)
        response = ScimResponse(document, status=201)
        response.headers.append((b"location", document["meta"]["location"].encode("utf-8")))
        return response

    async def write_user(request: Any, patching: bool) -> Response:
        org = org_of(request)
        base = base_of(request)
        found = await user_and_roles(org, request.path_params["id"])
        if found is None:
            return _error(404, "no such user in this organization")
        record, held = found
        body = await _json(request)
        if body is None:
            return _error(400, "request body is not valid JSON", "invalidSyntax")
        current = resources.user_document(record, roles=held, base=base)
        try:
            target = (
                apply_patch(current, body, shape=resources.USER)
                if patching
                else replace_document(current, body, shape=resources.USER)
            )
            record = await commit_user(org, record, current, target)
        except PatchError as error:
            return _refused(error)
        return ScimResponse(resources.user_document(record, roles=held, base=base))

    @router.put("/Users/{id}")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_replace_user(request: Any) -> Response:
        return await write_user(request, patching=False)

    @router.patch("/Users/{id}")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_patch_user(request: Any) -> Response:
        return await write_user(request, patching=True)

    @router.delete("/Users/{id}")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_delete_user(request: Any) -> Response:
        org = org_of(request)
        removed = await organizations.remove_member(org, request.path_params["id"])
        if not removed:
            return _error(404, "no such user in this organization")
        # The membership is gone; the account and everything it owns are not.
        # De-provisioning a directory user must not delete their content, and
        # cascading here would make an accidental one unrecoverable.
        return Response(b"", status=204)

    async def group_documents(org: str, base: str) -> list[dict[str, Any]]:
        held = await membership_map(org)
        return [
            resources.group_document(
                role,
                sorted(user_id for user_id, roles in held.items() if role in roles),
                base=base,
            )
            for role in sorted(organizations.roles())
        ]

    @router.get("/Groups")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_list_groups(request: Any) -> Response:
        org = org_of(request)
        query = query_of(request)
        try:
            return selected(await group_documents(org, base_of(request)), query, resources.GROUP)
        except FilterError as error:
            return _error(400, error.detail, "invalidFilter")
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)

    @router.get("/Groups/{id}")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_get_group(request: Any) -> Response:
        org = org_of(request)
        role = request.path_params["id"]
        if role not in organizations.roles():
            return _error(404, "no such group in this organization")
        held = await membership_map(org)
        return ScimResponse(
            resources.group_document(
                role,
                sorted(user_id for user_id, roles in held.items() if role in roles),
                base=base_of(request),
            )
        )

    async def write_group(request: Any, patching: bool) -> Response:
        org = org_of(request)
        base = base_of(request)
        role = request.path_params["id"]
        if role not in organizations.roles():
            return _error(404, "no such group in this organization")
        held = await membership_map(org)
        current = resources.group_document(
            role,
            sorted(user_id for user_id, roles in held.items() if role in roles),
            base=base,
        )
        body = await _json(request)
        if body is None:
            return _error(400, "request body is not valid JSON", "invalidSyntax")
        try:
            target = (
                apply_patch(current, body, shape=resources.GROUP)
                if patching
                else replace_document(current, body, shape=resources.GROUP)
            )
            await commit_group(org, role, target)
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)
        refreshed = await membership_map(org)
        return ScimResponse(
            resources.group_document(
                role,
                sorted(user_id for user_id, roles in refreshed.items() if role in roles),
                base=base,
            )
        )

    @router.put("/Groups/{id}")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_replace_group(request: Any) -> Response:
        return await write_group(request, patching=False)

    @router.patch("/Groups/{id}")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_patch_group(request: Any) -> Response:
        return await write_group(request, patching=True)

    @router.post("/Groups")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_create_group(request: Any) -> Response:
        return _error(
            501,
            "a group here is a role in this organization's declared vocabulary, "
            "which is configuration rather than data: it is declared where the "
            "OrganizationStore is constructed, and every Cedar policy naming it "
            "names it by string. Add the role there and it appears here",
        )

    @router.delete("/Groups/{id}")
    @authorize(action=write_action, resource=cedar_resource)
    async def scim_delete_group(request: Any) -> Response:
        return _error(
            501,
            "a group here is a role in this organization's declared vocabulary "
            "and cannot be deleted over SCIM; remove the members instead, or "
            "retire the role where the OrganizationStore is constructed",
        )

    return router


async def _json(request: Any) -> Any:
    """The request body as JSON, or `None` when it does not parse.

    `Request.json()` raises a bare `ValueError` that the pipeline would report
    as a 500, and a malformed body from a directory is a 400 with a SCIM error
    document. Narrow: `ValueError` is what the decoder raises, and nothing else
    here is being guarded.
    """
    try:
        return await request.json()
    except ValueError:
        return None
