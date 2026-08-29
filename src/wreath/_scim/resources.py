"""The mapping between SCIM's resource model and wreath's own.

RFC 7643 defines `User` and `Group`; wreath has a `UserStore` holding accounts
and an `OrganizationStore` holding memberships and a declared role vocabulary.
This module is the single place the two are reconciled, and every route reads
resources through it -- so a filter, a `PATCH` and a `GET` cannot form different
opinions about what `active` means.

## What maps onto what

| SCIM | wreath |
| --- | --- |
| `User.id` | `UserRecord.id` |
| `User.userName` | `UserRecord.email` |
| `User.emails` | `[UserRecord.email]`, read-only, derived |
| `User.active` | `UserRecord.is_active` |
| `User.groups` | the roles of this user's `Membership` in this organisation |
| `Group.id` / `Group.displayName` | a role name from `OrganizationStore.roles()` |
| `Group.members` | the users whose membership in this organisation carries it |

**A group is a role, not a second membership table.** That is the whole design
decision, and it falls out of the constraint rather than being chosen freely:
`wreath.organizations` already models "which users hold which named grants
inside one organisation", which is exactly what a SCIM group is. Giving SCIM its
own group store would have produced a second answer to that question, and
`context.org_roles` -- what policy actually reads -- would have kept reading the
first one.

The consequences are worth stating plainly, because they are visible to a
directory administrator:

* **Groups cannot be created or deleted over SCIM.** The role vocabulary is
  configuration, declared where the store is constructed, and a directory that
  could mint one would be writing application configuration over HTTP. Both
  verbs answer 501 and say so.
* **`Group.displayName` is immutable**, since renaming it would rename a role
  every Cedar policy in the deployment names by string.

## What has nowhere to go

`externalId`, `name.givenName`, `name.familyName`, `displayName`, `phoneNumbers`
and the rest of section 4.1's optional attributes have no home in
`wreath.users.UserRecord`, and inventing one would be the second user store this
whole design exists to avoid. They are therefore **not published in the schema,
not stored, and not filterable** -- a filter naming one is a 400 rather than an
empty page, because an empty page is what makes a directory conclude the user is
missing and create them a second time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "GROUP",
    "GROUP_SCHEMA",
    "GROUP_URN",
    "LIST_RESPONSE_URN",
    "SCHEMAS",
    "USER",
    "USER_SCHEMA",
    "USER_URN",
    "Shape",
    "error_document",
    "group_document",
    "list_response",
    "resource_types",
    "schema_documents",
    "service_provider_config",
    "user_document",
]

USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_URN = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_RESPONSE_URN = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_URN = "urn:ietf:params:scim:api:messages:2.0:Error"
SERVICE_PROVIDER_CONFIG_URN = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
RESOURCE_TYPE_URN = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCHEMA_URN = "urn:ietf:params:scim:schemas:core:2.0:Schema"


@dataclass(frozen=True, slots=True)
class Shape:
    """What one resource type's attributes are, and which of them a client may do what to.

    Every name here is lowercased, because SCIM attribute names are
    case-insensitive; `canonical` maps each back to the spelling that goes on
    the wire, so a `PATCH` naming `username` writes the same key a `GET`
    returns.

    Args:
        name: the resource type name, `"User"` or `"Group"`.
        endpoint: its collection path, `"/Users"` or `"/Groups"`.
        schema: the schema URN it declares.
        canonical: lowercase name -> wire spelling, for every attribute.
        writable: what a client may change. Everything else is `readOnly` and a
            `PATCH` naming it is refused with `mutability`.
        queryable: what a filter may name. `password` is writable and not
            queryable; `meta` is queryable and not writable.
        multi_valued: which attributes are lists.
    """

    name: str
    endpoint: str
    schema: str
    canonical: Mapping[str, str]
    writable: frozenset[str]
    queryable: frozenset[str]
    multi_valued: frozenset[str] = field(default_factory=frozenset)

    @property
    def attributes(self) -> frozenset[str]:
        """Every attribute name, lowercased."""
        return frozenset(self.canonical)

    def key(self, attribute: str) -> str:
        """The wire spelling of `attribute`, which is lowercased."""
        return self.canonical.get(attribute, attribute)


USER = Shape(
    name="User",
    endpoint="/Users",
    schema=USER_URN,
    canonical={
        "schemas": "schemas",
        "id": "id",
        "username": "userName",
        "active": "active",
        "password": "password",
        "emails": "emails",
        "groups": "groups",
        "meta": "meta",
    },
    writable=frozenset({"username", "active", "password"}),
    queryable=frozenset({"id", "username", "active", "emails", "groups", "meta"}),
    multi_valued=frozenset({"emails", "groups"}),
)

GROUP = Shape(
    name="Group",
    endpoint="/Groups",
    schema=GROUP_URN,
    canonical={
        "schemas": "schemas",
        "id": "id",
        "displayname": "displayName",
        "members": "members",
        "meta": "meta",
    },
    writable=frozenset({"members"}),
    queryable=frozenset({"id", "displayname", "members", "meta"}),
    multi_valued=frozenset({"members"}),
)


def _timestamp(epoch: float) -> str | None:
    """`epoch` as an RFC 3339 instant, or `None` when there is no timestamp.

    A record whose `created_at` is zero has no creation time recorded -- the
    field's default -- and 1970 is a fabrication rather than an answer, so the
    attribute is omitted instead.
    """
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _meta(
    shape: Shape, identifier: str, base: str, *, created: float = 0.0, changed: float = 0.0
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "resourceType": shape.name,
        "location": f"{base}{shape.endpoint}/{identifier}",
    }
    stamped = _timestamp(created)
    if stamped is not None:
        meta["created"] = stamped
    stamped = _timestamp(changed)
    if stamped is not None:
        meta["lastModified"] = stamped
    return meta


def user_document(record: Any, *, roles: Iterable[str], base: str) -> dict[str, Any]:
    """One user as SCIM reads it.

    `roles` are this user's roles *within the organisation this router serves*,
    unqualified -- the `"<org>:<role>"` spelling `wreath.organizations` produces
    for policy context is an authorization detail and never leaves here.
    """
    email = record.email
    return {
        "schemas": [USER_URN],
        "id": record.id,
        "userName": email,
        "active": bool(record.is_active),
        "emails": [{"value": email, "primary": True, "type": "work"}],
        "groups": [
            {
                "value": role,
                "display": role,
                "type": "direct",
                "$ref": f"{base}{GROUP.endpoint}/{role}",
            }
            for role in sorted(roles)
        ],
        "meta": _meta(
            USER,
            record.id,
            base,
            created=getattr(record, "created_at", 0.0),
            changed=getattr(record, "updated_at", 0.0),
        ),
    }


def group_document(role: str, members: Iterable[str], *, base: str) -> dict[str, Any]:
    """One role as SCIM reads it, over the user ids that hold it.

    A member carries no `display`, which section 4.2 makes optional. Filling it
    in would mean reading every member's account to render one list -- a page of
    groups turning into a query per member -- and nothing a directory does with
    a group needs the label.
    """
    return {
        "schemas": [GROUP_URN],
        "id": role,
        "displayName": role,
        "members": [
            {
                "value": user_id,
                "type": "User",
                "$ref": f"{base}{USER.endpoint}/{user_id}",
            }
            for user_id in members
        ],
        "meta": _meta(GROUP, role, base),
    }


def list_response(
    resources: list[dict[str, Any]], *, total: int, start_index: int, per_page: int
) -> dict[str, Any]:
    """The `ListResponse` envelope of section 3.4.2, with 1-based paging."""
    return {
        "schemas": [LIST_RESPONSE_URN],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": per_page,
        "Resources": resources,
    }


def error_document(status: int, detail: str, scim_type: str | None = None) -> dict[str, Any]:
    """The error document of section 3.12. `status` is a string on the wire."""
    body: dict[str, Any] = {"schemas": [ERROR_URN], "status": str(status)}
    if scim_type is not None:
        body["scimType"] = scim_type
    body["detail"] = detail
    return body


def service_provider_config(
    *, base: str, max_results: int, scheme: dict[str, Any]
) -> dict[str, Any]:
    """What this provider supports, per section 5 of RFC 7643.

    Every entry is a statement about *this* implementation rather than a copy of
    the specification's example. `bulk` is false because it is not implemented;
    `etag` is false because the resources have no version this
    provider can compute without a second source of truth for modification time.
    """
    return {
        "schemas": [SERVICE_PROVIDER_CONFIG_URN],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": max_results},
        "changePassword": {"supported": True},
        "sort": {"supported": True},
        "etag": {"supported": False},
        "authenticationSchemes": [scheme],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{base}/ServiceProviderConfig",
        },
    }


def resource_types(*, base: str) -> list[dict[str, Any]]:
    """The `ResourceType` documents of section 6, one per shape served."""
    return [
        {
            "schemas": [RESOURCE_TYPE_URN],
            "id": shape.name,
            "name": shape.name,
            "endpoint": shape.endpoint,
            "description": f"{shape.name} resources, as RFC 7643 defines them",
            "schema": shape.schema,
            "meta": {
                "resourceType": "ResourceType",
                "location": f"{base}/ResourceTypes/{shape.name}",
            },
        }
        for shape in (USER, GROUP)
    ]


def _attribute(
    name: str,
    kind: str,
    *,
    multi: bool = False,
    required: bool = False,
    mutability: str = "readWrite",
    returned: str = "default",
    uniqueness: str = "none",
    sub: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "type": kind,
        "multiValued": multi,
        "required": required,
        "caseExact": False,
        "mutability": mutability,
        "returned": returned,
        "uniqueness": uniqueness,
    }
    if sub is not None:
        entry["subAttributes"] = sub
    return entry


_MEMBER_SUB = [
    _attribute("value", "string", mutability="immutable"),
    _attribute("display", "string", mutability="immutable"),
    _attribute("type", "string", mutability="immutable"),
    _attribute("$ref", "reference", mutability="immutable"),
]

#: The published schemas. **These describe what is implemented, not what RFC
#: 7643 defines.** A schema advertising `name.givenName` on a provider that
#: drops it is a promise the next `GET` breaks, and a directory reads this
#: document to decide what to send.
USER_SCHEMA = {
    "schemas": [SCHEMA_URN],
    "id": USER_URN,
    "name": "User",
    "description": "A user account, as this provider stores one",
    "attributes": [
        _attribute("userName", "string", required=True, uniqueness="server"),
        _attribute("active", "boolean"),
        _attribute("password", "string", mutability="writeOnly", returned="never"),
        _attribute(
            "emails",
            "complex",
            multi=True,
            mutability="readOnly",
            sub=[
                _attribute("value", "string", mutability="readOnly"),
                _attribute("primary", "boolean", mutability="readOnly"),
                _attribute("type", "string", mutability="readOnly"),
            ],
        ),
        _attribute("groups", "complex", multi=True, mutability="readOnly", sub=_MEMBER_SUB),
    ],
}

GROUP_SCHEMA = {
    "schemas": [SCHEMA_URN],
    "id": GROUP_URN,
    "name": "Group",
    "description": "A role within this organisation; membership of it is a grant",
    "attributes": [
        _attribute("displayName", "string", required=True, mutability="readOnly"),
        _attribute("members", "complex", multi=True, sub=_MEMBER_SUB),
    ],
}

SCHEMAS = {USER_URN: USER_SCHEMA, GROUP_URN: GROUP_SCHEMA}


def schema_documents(*, base: str) -> list[dict[str, Any]]:
    """Both schema documents, each carrying the `meta` section 7 asks for."""
    return [
        {
            **document,
            "meta": {
                "resourceType": "Schema",
                "location": f"{base}/Schemas/{document['id']}",
            },
        }
        for document in (USER_SCHEMA, GROUP_SCHEMA)
    ]
