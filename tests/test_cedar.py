from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize


class Engine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def is_authorized(self, **request: object) -> bool:
        self.calls.append(request)
        return request["principal"] == "User::alice" and request["resource"] == "Document::42"


async def invoke(app: Wreath, token: str) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/documents/42",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_cedar_adapter_is_final_authorization_after_coarse_route_pruning() -> None:
    engine = Engine()

    async def verify(token: str) -> Identity | None:
        return Identity(token) if token in {"alice", "bob"} else None

    authorizer = CedarAuthorizer(
        engine=engine,
        principal=lambda identity: f"User::{identity.id}",
        action=lambda action, request: action,
        resource=lambda resource, request: f"Document::{resource}",
        entities=lambda request: (),
        context=lambda request: {"method": request.method},
    )
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), authorizer)

    @app.get("/documents/{document_id}")
    @authorize(action="Document::read", resource=lambda request: request.path_params["document_id"])
    async def document(request):
        return "allowed"

    allowed = await invoke(app, "alice")
    denied = await invoke(app, "bob")

    assert allowed[0]["status"] == 200
    assert denied[0]["status"] == 403
    # The context mapper here returns only `method`; `flags` is the authorizer's
    # own key and is supplied whatever the mapper does, empty when no provider is
    # configured. That is deliberate rather than incidental: an *absent* `flags`
    # makes `forbid ... unless { context.flags.contains(...) }` evaluate to
    # allowed -- the forbid is skipped rather than standing -- so a custom mapper
    # that omitted the key would silently disable every flag kill-switch in the
    # policy set. `tests/test_cedar_flags.py` pins that engine behaviour.
    assert engine.calls[0]["context"] == {"method": "GET", "flags": frozenset()}


# --- policy identity, delegated from the engine -------------------------------
#
# A cached permission manifest is tagged by the policy set behind the authorizer.
# That tag used to be found by reaching through `authorizer._engine` from
# `_auth/permissions.py` -- a private name owned by `_auth/cedar.py` and read
# from another module, where a rename would not raise but would silently drop
# every ETag to a per-instance token. Delegating keeps the name in the file that
# owns it, and hands out only the value.


class _Identified:
    """An engine that offers its policy text, the way `CedarPolicies` does."""

    def __init__(self, source: str) -> None:
        self.source = source

    def is_authorized(self, **request: object) -> bool:
        return False


class _Fingerprinted:
    """An engine that offers a digest instead of its text."""

    fingerprint = b"a-digest"

    def is_authorized(self, **request: object) -> bool:
        return False


def test_the_authorizer_offers_the_engines_policy_identity() -> None:
    authorizer = CedarAuthorizer(engine=_Identified("permit(principal, action, resource);"))

    assert authorizer.source == "permit(principal, action, resource);"


def test_every_probed_name_is_delegated_not_just_source() -> None:
    """Partial delegation would reintroduce the miss in a subtler form.

    An engine that offers `fingerprint` but not `source` has to be found through
    the authorizer too, or it silently degrades to a per-instance token while the
    engine next to it works fine.
    """
    assert CedarAuthorizer(engine=_Fingerprinted()).fingerprint == b"a-digest"


def test_an_engine_offering_nothing_leaves_the_names_absent() -> None:
    """Absence has to stay absence, not become a `None` that is present.

    The fingerprint probes with `getattr(..., None)` and falls through to a
    per-instance token. A property that always resolved and returned `None`
    would promise "there is a source, and it is nothing" -- a different claim,
    and one that would read as an engine having no policies at all.
    """
    authorizer = CedarAuthorizer(engine=Engine())

    for name in ("fingerprint", "source", "policies"):
        with pytest.raises(AttributeError):
            getattr(authorizer, name)
        assert getattr(authorizer, name, None) is None


def test_the_delegation_adds_no_dict_to_the_authorizer() -> None:
    """`CedarAuthorizer` is `__slots__`; properties must not have changed that."""
    authorizer = CedarAuthorizer(engine=_Identified("permit(principal, action, resource);"))

    assert not hasattr(authorizer, "__dict__")
    with pytest.raises(AttributeError):
        authorizer.source = "something else"  # type: ignore[misc]
