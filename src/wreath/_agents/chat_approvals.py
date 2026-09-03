from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping

from .._auth.models import qualified_identity_value
from ..chat import ChatContext, ChatOps, ChatReply
from .approvals import ApprovalGrant, ApprovalRequest, ApprovalStore

_APPROVE_PREFIX = "wreath:approval:approve:"
_DENY_PREFIX = "wreath:approval:deny:"
_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}").fullmatch
_PROVIDERS = frozenset({"discord", "slack", "teams"})


class ChatApprovalFlow:
    __slots__ = ("_approved", "_store")

    approve_prefix = _APPROVE_PREFIX
    deny_prefix = _DENY_PREFIX

    def __init__(self, chat: ChatOps, store: ApprovalStore) -> None:
        if not isinstance(chat, ChatOps):
            raise TypeError("chat approval flow requires ChatOps")
        if not isinstance(store, ApprovalStore):
            raise TypeError("chat approval flow requires an ApprovalStore")
        self._store = store
        self._approved: dict[str, Callable[[ChatContext, ApprovalGrant], Awaitable[ChatReply]]] = {}
        chat.action(self.approve_prefix, prefix=True)(self._approve)
        chat.action(self.deny_prefix, prefix=True)(self._deny)

    def on_approved(
        self,
        action: str,
        handler: Callable[[ChatContext, ApprovalGrant], Awaitable[ChatReply]],
    ) -> None:
        if not isinstance(action, str) or not action:
            raise ValueError("chat approved action must be a non-empty string")
        if not callable(handler) or not inspect.iscoroutinefunction(handler):
            raise TypeError("chat approved handler must be an async callable")
        if action in self._approved:
            raise ValueError(f"duplicate chat approved action: {action}")
        self._approved[action] = handler

    @property
    def schema_owners(self) -> tuple[ApprovalStore, ...]:
        return (self._store,)

    async def issue(
        self,
        context: ChatContext,
        *,
        approval_id: str,
        action: str,
        resource: str | None = None,
        ttl: float,
        require_fresh_auth: bool = False,
    ) -> ChatReply:
        provider = str(getattr(context, "provider", ""))
        if provider not in _PROVIDERS:
            raise ValueError(f"unsupported chat approval provider {provider!r}")
        self._validate_id(approval_id)
        tenant, principal_id = self._binding(context)
        request = await self._store.issue(
            approval_id=approval_id,
            tenant=tenant,
            principal_id=principal_id,
            action=action,
            resource=resource,
            ttl=ttl,
            require_fresh_auth=require_fresh_auth,
        )
        return self._render(provider, request)

    async def _approve(self, context: ChatContext) -> ChatReply:
        approval_id = self._action_id(context, self.approve_prefix)
        tenant, principal_id = self._binding(context)
        grant = await self._store.claim(
            approval_id,
            tenant=tenant,
            principal_id=principal_id,
            authenticated_at=self._authenticated_at(context),
        )
        handler = self._approved.get(grant.action) if isinstance(grant, ApprovalGrant) else None
        if handler is not None:
            return await handler(context, grant)
        return ChatReply.text("Approved.")

    async def _deny(self, context: ChatContext) -> ChatReply:
        approval_id = self._action_id(context, self.deny_prefix)
        tenant, principal_id = self._binding(context)
        await self._store.deny(
            approval_id,
            tenant=tenant,
            principal_id=principal_id,
        )
        return ChatReply.text("Denied.")

    @staticmethod
    def _validate_id(approval_id: str) -> None:
        if not isinstance(approval_id, str) or _APPROVAL_ID(approval_id) is None:
            raise ValueError(
                "approval ID must contain 1-64 ASCII letters, digits, underscores, or hyphens"
            )

    @classmethod
    def _action_id(cls, context: ChatContext, prefix: str) -> str:
        action = getattr(context, "action", None)
        if not isinstance(action, str) or not action.startswith(prefix):
            raise ValueError(f"chat approval action must start with {prefix!r}")
        approval_id = action[len(prefix) :]
        cls._validate_id(approval_id)
        return approval_id

    @staticmethod
    def _binding(context: ChatContext) -> tuple[str, str]:
        tenant = getattr(context, "tenant", None)
        identity = getattr(context, "identity", None)
        identity_id = getattr(identity, "id", None)
        identity_key = qualified_identity_value(
            str(getattr(identity, "namespace", "")), str(identity_id)
        )
        principal = getattr(context, "principal", None)
        principal_identity = getattr(principal, "identity", None)
        linked_identity_id = getattr(principal_identity, "id", None)
        if linked_identity_id is None:
            principal_id = getattr(principal, "id", None)
            principal_key = None
        else:
            principal_id = linked_identity_id
            principal_key = qualified_identity_value(
                str(getattr(principal_identity, "namespace", "")), str(principal_id)
            )
        if not tenant or not identity_id or principal is None:
            raise LookupError("chat approval requires a linked identity")
        if principal_id is not None and (
            principal_key != identity_key
            if principal_key is not None
            else str(principal_id) != str(identity_id)
        ):
            raise LookupError("chat approval linked principal does not match its identity")
        return str(tenant), identity_key

    @staticmethod
    def _authenticated_at(context: ChatContext) -> float | None:
        identity = getattr(context, "identity", None)
        claims = getattr(identity, "claims", None)
        if not isinstance(claims, Mapping):
            return None
        stamps = (
            value
            for key in ("auth_time", "second_factor_at")
            if isinstance((value := claims.get(key)), (int, float)) and not isinstance(value, bool)
        )
        return max(stamps, default=None)

    def _render(self, provider: str, request: ApprovalRequest) -> ChatReply:
        approve = f"{self.approve_prefix}{request.approval_id}"
        deny = f"{self.deny_prefix}{request.approval_id}"
        text = f"Approve {request.action}"
        if request.resource is not None:
            text = f"{text} for {request.resource}"
        text = f"{text}?"
        if provider == "slack":
            return ChatReply.ephemeral(
                text,
                blocks=(
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    {
                        "type": "actions",
                        "elements": (
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Approve"},
                                "style": "primary",
                                "action_id": approve,
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Deny"},
                                "style": "danger",
                                "action_id": deny,
                            },
                        ),
                    },
                ),
            )
        if provider == "teams":
            return ChatReply.card(
                {
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                    "actions": [
                        {"type": "Action.Execute", "title": "Approve", "verb": approve},
                        {"type": "Action.Execute", "title": "Deny", "verb": deny},
                    ],
                }
            )
        return ChatReply.ephemeral(
            text,
            native={
                "flags": 64,
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 3,
                                "label": "Approve",
                                "custom_id": approve,
                            },
                            {
                                "type": 2,
                                "style": 4,
                                "label": "Deny",
                                "custom_id": deny,
                            },
                        ],
                    }
                ],
            },
        )


__all__ = ["ChatApprovalFlow"]
