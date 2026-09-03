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
import hashlib
import hmac
import inspect
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import Any

from .._b64 import b64url_decode, b64url_encode
from .._codecs import parse_qs
from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from .._userkit import hash_password
from ..authorization import authorize
from ..cache import BoundedCache
from ..response import JSONResponse, Response
from ..router import Router
from . import resources
from .filters import FilterError, select
from .filters import _compile as compile_filter
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
_SEARCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:SearchRequest"
_CURSOR_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


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
        node = compile_filter(parse_filter(expression, attributes=attributes))
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


def _integer(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise PatchError("invalidValue", f"{name} must be an integer (got {raw!r})") from None


@dataclass(frozen=True, slots=True)
class _ScimBuildContext:
    values: dict[str, Any]


def _scim_data_helpers(
    *,
    app: Any,
    users: Any,
    organizations: Any,
    resolve: Callable[[Any], str],
    prefix: str,
) -> dict[str, Any]:
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

    return {
        "org_of": org_of,
        "cedar_resource": cedar_resource,
        "base_of": base_of,
        "membership_map": membership_map,
        "roles_in": roles_in,
        "user_and_roles": user_and_roles,
        "find_by_user_name": find_by_user_name,
        "hashed": hashed,
    }


def _scim_write_helpers(
    *,
    users: Any,
    organizations: Any,
    data_helpers: dict[str, Any],
    revoke_sessions: Callable[[str], Any] | None,
) -> dict[str, Any]:
    find_by_user_name = data_helpers["find_by_user_name"]
    hashed = data_helpers["hashed"]
    membership_map = data_helpers["membership_map"]

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
        if _session_revocation_required(current, target):
            if revoke_sessions is None:
                raise PatchError(
                    "mutability",
                    "changing active or password requires scim_router(revoke_sessions=...) "
                    "so already-issued sessions are invalidated",
                )
            revoked = revoke_sessions(str(record.id))
            if inspect.isawaitable(revoked):
                await revoked
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

    return {
        "commit_user": commit_user,
        "commit_group": commit_group,
    }


def _session_revocation_required(
    current: Mapping[str, Any], target: Mapping[str, Any]
) -> bool:
    disabled = bool(current.get("active")) and target.get("active") is False
    return disabled or target.get("password") is not None


def _scim_query_helpers(
    *,
    page_size: int,
    max_page_size: int,
) -> dict[str, Any]:
    def query_of(request: Any) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, value in parse_qs(request.query_string):
            values.setdefault(key.lower(), value)
        return values

    async def search_query_of(request: Any) -> dict[str, str]:
        body = await _json(request)
        if not isinstance(body, Mapping):
            raise PatchError("invalidSyntax", "SCIM search body must be a JSON object")
        normalized: dict[str, Any] = {}
        for raw_name, value in body.items():
            if not isinstance(raw_name, str):
                raise PatchError("invalidSyntax", "SCIM search member names must be strings")
            name = raw_name.lower()
            if name in normalized:
                raise PatchError(
                    "invalidSyntax",
                    f"SCIM search contains {raw_name!r} more than once ignoring case",
                )
            normalized[name] = value
        schemas = normalized.pop("schemas", None)
        if schemas != [_SEARCH_SCHEMA]:
            raise PatchError(
                "invalidSyntax",
                f"SCIM search schemas must be exactly [{_SEARCH_SCHEMA!r}]",
            )
        unsupported = {"attributes", "excludedattributes"}.intersection(normalized)
        if unsupported:
            raise PatchError(
                "invalidValue",
                "this SCIM provider does not implement response projection; omit "
                + ", ".join(sorted(unsupported)),
            )
        allowed = {"filter", "sortby", "sortorder", "startindex", "count", "cursor"}
        unknown = set(normalized).difference(allowed)
        if unknown:
            raise PatchError(
                "invalidSyntax",
                "SCIM search contains unknown member(s): " + ", ".join(sorted(unknown)),
            )
        query: dict[str, str] = {}
        for name, value in normalized.items():
            if name in {"startindex", "count"}:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise PatchError("invalidValue", f"SCIM search {name} must be an integer")
                query[name] = str(value)
            elif not isinstance(value, str):
                raise PatchError("invalidValue", f"SCIM search {name} must be a string")
            else:
                query[name] = value
        return query

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

    return {
        "query_of": query_of,
        "search_query_of": search_query_of,
        "sort_documents": sort_documents,
        "paging": paging,
    }


def _scim_cursor_helpers(
    *,
    page_size: int,
    max_page_size: int,
    cursor_timeout: int,
    cursor_key: bytes,
) -> dict[str, Any]:
    def cursor_count(query: Mapping[str, str]) -> int:
        count = page_size
        if "count" in query:
            try:
                count = max(0, int(query["count"]))
            except ValueError:
                raise PatchError(
                    "invalidCount",
                    f"cursor count must be an integer (got {query['count']!r})",
                ) from None
        if count > max_page_size:
            raise PatchError(
                "invalidCount",
                f"cursor count must be between 0 and {max_page_size} (got {count})",
            )
        return count

    def cursor_query(query: Mapping[str, str]) -> str:
        stable = {key: value for key, value in query.items() if key not in {"cursor", "count"}}
        encoded = _json_dumps(dict(sorted(stable.items())))
        return b64url_encode(hashlib.sha256(encoded).digest())

    def encode_cursor(
        position: int,
        *,
        count: int,
        query: Mapping[str, str],
        shape: resources.Shape,
        organization_id: str,
    ) -> str:
        payload = b64url_encode(
            _json_dumps(
                [
                    1,
                    position,
                    int(time.time()) + cursor_timeout,
                    shape.name,
                    organization_id,
                    count,
                    cursor_query(query),
                ]
            )
        )
        tag = b64url_encode(hmac.new(cursor_key, payload.encode("ascii"), hashlib.sha256).digest())
        return f"{payload}.{tag}"

    def decode_cursor(
        value: str,
        *,
        count: int,
        query: Mapping[str, str],
        shape: resources.Shape,
        organization_id: str,
    ) -> int:
        if len(value) > 2048 or not value or any(char not in _CURSOR_ALPHABET for char in value):
            raise PatchError("invalidCursor", "cursor is not an opaque URL-safe value issued here")
        payload, separator, tag = value.partition(".")
        if not separator or "." in tag:
            raise PatchError("invalidCursor", "cursor is not an opaque value issued here")
        expected = b64url_encode(
            hmac.new(cursor_key, payload.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(tag, expected):
            raise PatchError("invalidCursor", "cursor signature is invalid")
        try:
            decoded = _json_loads(b64url_decode(payload))
        except TypeError, ValueError:
            raise PatchError("invalidCursor", "cursor payload is malformed") from None
        if not isinstance(decoded, list) or len(decoded) != 7:
            raise PatchError("invalidCursor", "cursor payload has the wrong shape")
        version, position, expires, resource_name, cursor_org, original_count, fingerprint = decoded
        if (
            version != 1
            or isinstance(position, bool)
            or not isinstance(position, int)
            or position < 0
        ):
            raise PatchError("invalidCursor", "cursor position is invalid")
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise PatchError("invalidCursor", "cursor expiry is invalid")
        if expires < int(time.time()):
            raise PatchError("expiredCursor", "cursor has expired; begin a new cursor query")
        if original_count != count:
            raise PatchError(
                "invalidCount",
                f"cursor count must remain {original_count} for every page (got {count})",
            )
        if (
            resource_name != shape.name
            or cursor_org != organization_id
            or fingerprint != cursor_query(query)
        ):
            raise PatchError(
                "invalidCursor",
                "cursor belongs to a different resource, organization, or query",
            )
        return position

    def cursor_window(
        documents: list[dict[str, Any]],
        query: Mapping[str, str],
        shape: resources.Shape,
        organization_id: str,
    ) -> ScimResponse:
        if "startindex" in query:
            raise PatchError(
                "invalidCursor", "use cursor or startIndex pagination, not both in one request"
            )
        count = cursor_count(query)
        raw_cursor = query.get("cursor", "")
        position = (
            decode_cursor(
                raw_cursor,
                count=count,
                query=query,
                shape=shape,
                organization_id=organization_id,
            )
            if raw_cursor
            else 0
        )
        window = documents[position : position + count]
        next_position = position + len(window)
        next_cursor = ""
        if count and next_position < len(documents):
            next_cursor = encode_cursor(
                next_position,
                count=count,
                query=query,
                shape=shape,
                organization_id=organization_id,
            )
        return ScimResponse(
            resources.cursor_list_response(
                window,
                total=len(documents),
                next_cursor=next_cursor,
            )
        )

    return {
        "cursor_count": cursor_count,
        "cursor_query": cursor_query,
        "encode_cursor": encode_cursor,
        "decode_cursor": decode_cursor,
        "cursor_window": cursor_window,
    }


def _scim_selection_helper(
    *,
    user_filter_cache: BoundedCache,
    group_filter_cache: BoundedCache,
    query_helpers: dict[str, Any],
    cursor_helpers: dict[str, Any],
) -> Callable[[list[dict[str, Any]], Mapping[str, str], resources.Shape, str], ScimResponse]:
    sort_documents = query_helpers["sort_documents"]
    paging = query_helpers["paging"]
    cursor_window = cursor_helpers["cursor_window"]

    def selected(
        documents: list[dict[str, Any]],
        query: Mapping[str, str],
        shape: resources.Shape,
        organization_id: str,
    ) -> ScimResponse:
        """Filter, page and envelope a list of already-built representations."""
        expression = query.get("filter")
        if expression:
            cache = user_filter_cache if shape is resources.USER else group_filter_cache
            node = _parsed_filter(cache, expression, shape.queryable)
            documents = select(node, documents)
        documents = sort_documents(documents, query, shape)
        if "cursor" in query:
            return cursor_window(documents, query, shape, organization_id)
        start, count = paging(query)
        window = documents[start - 1 : start - 1 + count]
        return ScimResponse(
            resources.list_response(
                window, total=len(documents), start_index=start, per_page=len(window)
            )
        )

    return selected


def _scim_authentication_helper(app: Any) -> Callable[[Any], dict[str, Any]]:
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

    return authentication_scheme


def _make_scim_context(
    *,
    router: Router,
    app: Any,
    users: Any,
    organizations: Any,
    resolve: Callable[[Any], str],
    prefix: str,
    read_action: str,
    write_action: str,
    page_size: int,
    max_page_size: int,
    max_filter_scan: int,
    cursor_timeout: int,
    cursor_key: bytes,
    user_filter_cache: BoundedCache,
    group_filter_cache: BoundedCache,
    revoke_sessions: Callable[[str], Any] | None,
) -> _ScimBuildContext:
    values = {
        **(
            data_helpers := _scim_data_helpers(
                app=app,
                users=users,
                organizations=organizations,
                resolve=resolve,
                prefix=prefix,
            )
        ),
        **_scim_write_helpers(
            users=users,
            organizations=organizations,
            data_helpers=data_helpers,
            revoke_sessions=revoke_sessions,
        ),
        **(
            query_helpers := _scim_query_helpers(
                page_size=page_size,
                max_page_size=max_page_size,
            )
        ),
        **(
            cursor_helpers := _scim_cursor_helpers(
                page_size=page_size,
                max_page_size=max_page_size,
                cursor_timeout=cursor_timeout,
                cursor_key=cursor_key,
            )
        ),
        "selected": _scim_selection_helper(
            user_filter_cache=user_filter_cache,
            group_filter_cache=group_filter_cache,
            query_helpers=query_helpers,
            cursor_helpers=cursor_helpers,
        ),
        "authentication_scheme": _scim_authentication_helper(app),
        "router": router,
        "app": app,
        "users": users,
        "organizations": organizations,
        "prefix": prefix,
        "read_action": read_action,
        "write_action": write_action,
        "page_size": page_size,
        "max_page_size": max_page_size,
        "max_filter_scan": max_filter_scan,
        "cursor_timeout": cursor_timeout,
        "_integer": _integer,
    }
    return _ScimBuildContext(values)


def _mount_scim_discovery(context: _ScimBuildContext) -> None:
    values = context.values
    (
        _org_of,
        cedar_resource,
        base_of,
        _membership_map,
        _roles_in,
        _user_and_roles,
        _find_by_user_name,
        _hashed,
        _commit_user,
        _commit_group,
        _query_of,
        _search_query_of,
        _sort_documents,
        _paging,
        _cursor_count,
        _cursor_query,
        _encode_cursor,
        _decode_cursor,
        _cursor_window,
        _integer,
        _selected,
        authentication_scheme,
        router,
        _app,
        _users,
        _organizations,
        _prefix,
        read_action,
        _write_action,
        page_size,
        max_page_size,
        max_filter_scan,
        cursor_timeout,
    ) = (
        values["org_of"],
        values["cedar_resource"],
        values["base_of"],
        values["membership_map"],
        values["roles_in"],
        values["user_and_roles"],
        values["find_by_user_name"],
        values["hashed"],
        values["commit_user"],
        values["commit_group"],
        values["query_of"],
        values["search_query_of"],
        values["sort_documents"],
        values["paging"],
        values["cursor_count"],
        values["cursor_query"],
        values["encode_cursor"],
        values["decode_cursor"],
        values["cursor_window"],
        values["_integer"],
        values["selected"],
        values["authentication_scheme"],
        values["router"],
        values["app"],
        values["users"],
        values["organizations"],
        values["prefix"],
        values["read_action"],
        values["write_action"],
        values["page_size"],
        values["max_page_size"],
        values["max_filter_scan"],
        values["cursor_timeout"],
    )

    @router.get("/ServiceProviderConfig")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_service_provider_config(request: Any) -> Response:
        return ScimResponse(
            resources.service_provider_config(
                base=base_of(request),
                max_results=max_filter_scan,
                scheme=authentication_scheme(request),
                default_page_size=page_size,
                max_page_size=max_page_size,
                cursor_timeout=cursor_timeout,
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


def _mount_scim_users(context: _ScimBuildContext) -> None:
    values = context.values
    (
        org_of,
        cedar_resource,
        base_of,
        membership_map,
        _roles_in,
        _user_and_roles,
        _find_by_user_name,
        _hashed,
        _commit_user,
        _commit_group,
        query_of,
        search_query_of,
        _sort_documents,
        paging,
        cursor_count,
        _cursor_query,
        encode_cursor,
        decode_cursor,
        _cursor_window,
        _integer,
        selected,
        _authentication_scheme,
        router,
        _app,
        users,
        _organizations,
        _prefix,
        read_action,
        _write_action,
        _page_size,
        _max_page_size,
        max_filter_scan,
        _cursor_timeout,
    ) = (
        values["org_of"],
        values["cedar_resource"],
        values["base_of"],
        values["membership_map"],
        values["roles_in"],
        values["user_and_roles"],
        values["find_by_user_name"],
        values["hashed"],
        values["commit_user"],
        values["commit_group"],
        values["query_of"],
        values["search_query_of"],
        values["sort_documents"],
        values["paging"],
        values["cursor_count"],
        values["cursor_query"],
        values["encode_cursor"],
        values["decode_cursor"],
        values["cursor_window"],
        values["_integer"],
        values["selected"],
        values["authentication_scheme"],
        values["router"],
        values["app"],
        values["users"],
        values["organizations"],
        values["prefix"],
        values["read_action"],
        values["write_action"],
        values["page_size"],
        values["max_page_size"],
        values["max_filter_scan"],
        values["cursor_timeout"],
    )

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
        ordered = sorted(held)
        if not ordered:
            return []
        records = await users.get_many_by_id(ordered)
        if len(records) != len(ordered):
            raise RuntimeError(
                f"{type(users).__name__}.get_many_by_id must return one result per id "
                f"({len(ordered)} ids, {len(records)} results)"
            )
        return [
            resources.user_document(record, roles=held[user_id], base=base)
            for user_id, record in zip(ordered, records, strict=True)
            if record is not None
        ]

    async def search_users(request: Any, query: Mapping[str, str]) -> Response:
        org = org_of(request)
        base = base_of(request)
        try:
            if query.get("filter") or query.get("sortby"):
                return selected(
                    await user_documents(org, base, max_filter_scan),
                    query,
                    resources.USER,
                    org,
                )
            # Unfiltered: page over the membership list and read only the users
            # on the page, so an organisation larger than one page costs one
            # page of lookups rather than all of them.
            held = await membership_map(org)
            ordered = sorted(held)
            if "cursor" in query:
                if "startindex" in query:
                    raise PatchError(
                        "invalidCursor",
                        "use cursor or startIndex pagination, not both in one request",
                    )
                count = cursor_count(query)
                raw_cursor = query.get("cursor", "")
                position = (
                    decode_cursor(
                        raw_cursor,
                        count=count,
                        query=query,
                        shape=resources.USER,
                        organization_id=org,
                    )
                    if raw_cursor
                    else 0
                )
                page = ordered[position : position + count]
                start = position + 1
            else:
                start, count = paging(query)
                position = start - 1
                page = ordered[position : position + count]
            records = await users.get_many_by_id(page) if page else []
            if len(records) != len(page):
                raise RuntimeError(
                    f"{type(users).__name__}.get_many_by_id must return one result per id "
                    f"({len(page)} ids, {len(records)} results)"
                )
            found = [
                resources.user_document(record, roles=held[user_id], base=base)
                for user_id, record in zip(page, records, strict=True)
                if record is not None
            ]
            if "cursor" in query:
                next_position = position + len(page)
                next_cursor = ""
                if count and next_position < len(ordered):
                    next_cursor = encode_cursor(
                        next_position,
                        count=count,
                        query=query,
                        shape=resources.USER,
                        organization_id=org,
                    )
                return ScimResponse(
                    resources.cursor_list_response(
                        found,
                        total=len(ordered),
                        next_cursor=next_cursor,
                    )
                )
            return ScimResponse(
                resources.list_response(
                    found,
                    total=len(ordered),
                    start_index=start,
                    per_page=len(found),
                )
            )
        except FilterError as error:
            return _error(400, error.detail, "invalidFilter")
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)

    @router.get("/Users")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_list_users(request: Any) -> Response:
        return await search_users(request, query_of(request))

    @router.post("/Users/.search")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_search_users(request: Any) -> Response:
        try:
            query = await search_query_of(request)
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)
        return await search_users(request, query)


def _mount_scim_user_crud(context: _ScimBuildContext) -> None:
    values = context.values
    (
        org_of,
        cedar_resource,
        base_of,
        _membership_map,
        roles_in,
        user_and_roles,
        find_by_user_name,
        _hashed,
        commit_user,
        _commit_group,
        _query_of,
        _search_query_of,
        _sort_documents,
        _paging,
        _cursor_count,
        _cursor_query,
        _encode_cursor,
        _decode_cursor,
        _cursor_window,
        _integer,
        _selected,
        _authentication_scheme,
        router,
        _app,
        users,
        organizations,
        _prefix,
        read_action,
        write_action,
        _page_size,
        _max_page_size,
        _max_filter_scan,
        _cursor_timeout,
    ) = (
        values["org_of"],
        values["cedar_resource"],
        values["base_of"],
        values["membership_map"],
        values["roles_in"],
        values["user_and_roles"],
        values["find_by_user_name"],
        values["hashed"],
        values["commit_user"],
        values["commit_group"],
        values["query_of"],
        values["search_query_of"],
        values["sort_documents"],
        values["paging"],
        values["cursor_count"],
        values["cursor_query"],
        values["encode_cursor"],
        values["decode_cursor"],
        values["cursor_window"],
        values["_integer"],
        values["selected"],
        values["authentication_scheme"],
        values["router"],
        values["app"],
        values["users"],
        values["organizations"],
        values["prefix"],
        values["read_action"],
        values["write_action"],
        values["page_size"],
        values["max_page_size"],
        values["max_filter_scan"],
        values["cursor_timeout"],
    )

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


def _mount_scim_groups(context: _ScimBuildContext) -> None:
    values = context.values
    (
        org_of,
        cedar_resource,
        base_of,
        membership_map,
        _roles_in,
        _user_and_roles,
        _find_by_user_name,
        _hashed,
        _commit_user,
        commit_group,
        query_of,
        search_query_of,
        _sort_documents,
        _paging,
        _cursor_count,
        _cursor_query,
        _encode_cursor,
        _decode_cursor,
        _cursor_window,
        _integer,
        selected,
        _authentication_scheme,
        router,
        _app,
        _users,
        organizations,
        _prefix,
        read_action,
        write_action,
        _page_size,
        _max_page_size,
        _max_filter_scan,
        _cursor_timeout,
    ) = (
        values["org_of"],
        values["cedar_resource"],
        values["base_of"],
        values["membership_map"],
        values["roles_in"],
        values["user_and_roles"],
        values["find_by_user_name"],
        values["hashed"],
        values["commit_user"],
        values["commit_group"],
        values["query_of"],
        values["search_query_of"],
        values["sort_documents"],
        values["paging"],
        values["cursor_count"],
        values["cursor_query"],
        values["encode_cursor"],
        values["decode_cursor"],
        values["cursor_window"],
        values["_integer"],
        values["selected"],
        values["authentication_scheme"],
        values["router"],
        values["app"],
        values["users"],
        values["organizations"],
        values["prefix"],
        values["read_action"],
        values["write_action"],
        values["page_size"],
        values["max_page_size"],
        values["max_filter_scan"],
        values["cursor_timeout"],
    )

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

    async def search_groups(request: Any, query: Mapping[str, str]) -> Response:
        org = org_of(request)
        try:
            return selected(
                await group_documents(org, base_of(request)), query, resources.GROUP, org
            )
        except FilterError as error:
            return _error(400, error.detail, "invalidFilter")
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)

    @router.get("/Groups")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_list_groups(request: Any) -> Response:
        return await search_groups(request, query_of(request))

    @router.post("/Groups/.search")
    @authorize(action=read_action, resource=cedar_resource)
    async def scim_search_groups(request: Any) -> Response:
        try:
            query = await search_query_of(request)
        except PatchError as error:
            return _error(400, error.detail, error.scim_type)
        return await search_groups(request, query)

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
    cursor_secret: bytes | str | None = None,
    cursor_timeout: int = 3600,
    revoke_sessions: Callable[[str], Any] | None = None,
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
    | `POST {prefix}/Users/.search` | list with body-carried search parameters |
    | `GET/PUT/PATCH/DELETE {prefix}/Users/{id}` | one member |
    | `GET {prefix}/Groups[/{id}]` | the organisation's roles |
    | `POST {prefix}/Groups/.search` | list roles with body-carried search parameters |
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
        users: a `wreath.users` `UserStore` -- `get_by_id`, `get_many_by_id`,
            `get_by_email`, `create`, `update`.
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
            building the representations reads their accounts in one batch, so
            this bounds the work one request can ask for.
        cursor_secret: key authenticating opaque cursors. A single-process
            deployment may omit it; every worker in a fleet must share one.
        cursor_timeout: seconds a cursor remains valid between page requests.

    Returns:
        A `Router` to pass to `app.include_router`.

    Raises:
        ValueError: a store missing a method, an application with no authorizer,
            an empty action name, an inconsistent page size, or an organisation
            id that cannot be spelled inside a Cedar entity reference.
    """
    if cursor_timeout <= 0:
        raise ValueError("scim_router cursor_timeout must be positive")
    supplied_cursor_secret = (
        cursor_secret.encode("utf-8") if isinstance(cursor_secret, str) else cursor_secret
    )
    if supplied_cursor_secret is not None and len(supplied_cursor_secret) < 32:
        raise ValueError("scim_router cursor_secret must contain at least 32 bytes")
    cursor_key = supplied_cursor_secret or secrets.token_bytes(32)
    _require(
        users,
        ("get_by_id", "get_many_by_id", "get_by_email", "create", "update"),
        "user store",
    )
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

    router = Router(prefix=prefix, tags=("scim",))
    user_filter_cache = BoundedCache(max_entries=_FILTER_CACHE_SIZE)
    group_filter_cache = BoundedCache(max_entries=_FILTER_CACHE_SIZE)
    context = _make_scim_context(
        router=router,
        app=app,
        users=users,
        organizations=organizations,
        resolve=resolve,
        prefix=prefix,
        read_action=read_action,
        write_action=write_action,
        page_size=page_size,
        max_page_size=max_page_size,
        max_filter_scan=max_filter_scan,
        cursor_timeout=cursor_timeout,
        cursor_key=cursor_key,
        user_filter_cache=user_filter_cache,
        group_filter_cache=group_filter_cache,
        revoke_sessions=revoke_sessions,
    )
    _mount_scim_discovery(context)
    _mount_scim_users(context)
    _mount_scim_user_crud(context)
    _mount_scim_groups(context)
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
