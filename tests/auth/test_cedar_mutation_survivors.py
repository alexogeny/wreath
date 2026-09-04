from __future__ import annotations

from wreath._auth.cedar import CedarAuthorizer
from wreath._auth.cedar_engine import CedarEntity, EntityUid
from wreath._auth.models import Identity
from wreath._auth.requirements import PolicyRequirement
from wreath.request import Request


async def test_opaque_action_does_not_guard_resource_entity_uids() -> None:
    class OpaqueAction:
        def __eq__(self, other: object) -> bool:
            return True

    class Engine:
        def is_authorized(self, **arguments: object) -> bool:
            return True

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    action = OpaqueAction()
    resource = CedarEntity(EntityUid("Document", "42"))
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": []},
        receive,
    )
    request._set_identity(Identity("alice"))
    authorizer = CedarAuthorizer(
        engine=Engine(),
        action=lambda action_name, request: action,
    )

    decision = await authorizer.authorize(request, PolicyRequirement("read", resource))

    assert decision.allowed
