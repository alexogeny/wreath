from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wreath.auth import Identity
from wreath.authorization import (
    AuthorizationDecision,
    CedarAuthorizer,
    CedarPolicies,
    EntityUid,
    human,
    member_of,
)
from wreath.chat import (
    ExternalIdentityKey,
    ExternalIdentityResolver,
    IdentityResolutionError,
    PrincipalBinding,
)
from wreath.chat.teams import Teams, TeamsActivity, TeamsBotConfig

from ._support import (
    AAD_OBJECT_ID,
    APP_ID,
    ENTRA_TENANT,
    MemoryInbox,
    RecordingJobs,
    activity,
)
from .test_chatops_contract import mounted, post

ENTRA_ISSUER = f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"


@dataclass(slots=True)
class BindingStore:
    rows: list[tuple[ExternalIdentityKey, PrincipalBinding]]
    lookups: list[ExternalIdentityKey]

    async def lookup(self, key: ExternalIdentityKey) -> tuple[PrincipalBinding, ...]:
        self.lookups.append(key)
        return tuple(binding for candidate, binding in self.rows if candidate == key)


def binding() -> PrincipalBinding:
    identity = Identity(
        "user-7",
        roles=frozenset({"acme:operator"}),
        permissions=frozenset({"Deploy::run"}),
        claims={"second_factor_at": 1_799_999_900, "acr": "urn:wreath:mfa"},
        attributes={"account_status": "active"},
    )
    principal = human(identity) | member_of("acme", role="operator")
    return PrincipalBinding(
        identity=identity,
        principal=principal,
        tenant="acme",
        external=ExternalIdentityKey(
            issuer=ENTRA_ISSUER,
            subject=AAD_OBJECT_ID,
            tenant=ENTRA_TENANT,
        ),
    )


def teams(**changes: Any) -> Teams:
    values = {
        "config": TeamsBotConfig(
            app_id=APP_ID,
            app_secret="secret",
            messaging_endpoint="https://chat.example.test/teams/activities",
            allowed_tenants=frozenset({ENTRA_TENANT}),
            login_issuers={ENTRA_TENANT: ENTRA_ISSUER},
        )
    }
    values.update(changes)
    return Teams(**values)


async def test_durable_enqueue_and_worker_share_the_canonical_principal_scope() -> None:
    expected = binding()
    store = BindingStore([(expected.external, expected)], [])
    resolver = ExternalIdentityResolver(store=store)
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(
        identity=resolver,
        jobs=jobs,
        inbox=MemoryInbox(),
        login_issuers={ENTRA_TENANT: ENTRA_ISSUER},
    )
    seen: list[Any] = []

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        seen.append(request)

    assert (await post(app, activity())).status == 200
    assert jobs.pending[0][2]["tenant"] == "acme"
    await jobs.run_next()

    assert seen[0].tenant == "acme"
    assert seen[0].principal is expected.principal
    assert store.lookups == [expected.external, expected.external]


async def test_verified_teams_identity_resolves_the_existing_wreath_principal() -> None:
    expected = binding()
    store = BindingStore([(expected.external, expected)], [])
    resolver = ExternalIdentityResolver(store=store)
    parsed = TeamsActivity.parse(activity())

    external = teams().external_identity(parsed)
    resolved = await resolver.resolve(external)

    assert resolved is expected
    assert resolved.identity is expected.identity
    assert resolved.principal is expected.principal
    assert resolved.tenant == "acme"
    assert resolved.identity.roles == frozenset({"acme:operator"})
    assert resolved.identity.permissions == frozenset({"Deploy::run"})
    assert resolved.principal.limits.active_organization == "acme"
    assert resolved.identity.claims["second_factor_at"] == 1_799_999_900
    assert store.lookups == [
        ExternalIdentityKey(
            issuer=ENTRA_ISSUER,
            subject=AAD_OBJECT_ID,
            tenant=ENTRA_TENANT,
        )
    ]


async def test_federation_does_not_create_a_second_user() -> None:
    expected = binding()
    store = BindingStore([(expected.external, expected)], [])
    resolver = ExternalIdentityResolver(store=store)

    first_key = teams().external_identity(TeamsActivity.parse(activity(id="a1")))
    second_key = teams().external_identity(TeamsActivity.parse(activity(id="a2")))
    first = await resolver.resolve(first_key)
    second = await resolver.resolve(second_key)

    assert first is expected
    assert second is expected
    assert len(store.rows) == 1


async def test_resolved_principal_binds_the_same_identity_cedar_sees_after_normal_login() -> None:
    expected = binding()
    resolver = ExternalIdentityResolver(store=BindingStore([(expected.external, expected)], []))
    external = teams().external_identity(TeamsActivity.parse(activity()))
    resolved = await resolver.resolve(external)

    assert resolved.principal.bind() == expected.principal.bind()
    assert EntityUid("User", resolved.principal.bind().id) == EntityUid("User", "user-7")


@pytest.mark.parametrize(
    ("payload", "config_changes", "reason"),
    [
        (
            activity(
                channelData={"tenant": {"id": "unconfigured"}, "team": {"id": "team"}},
                conversation={
                    "id": "conversation",
                    "conversationType": "channel",
                    "tenantId": "unconfigured",
                },
            ),
            {},
            "unconfigured-issuer",
        ),
        (
            activity(**{"from": {"id": "teams-user", "name": "user@example.test"}}),
            {},
            "missing-external-subject",
        ),
    ],
)
async def test_unverified_or_unconfigured_external_identity_never_falls_back_to_profile_fields(
    payload: dict[str, Any], config_changes: dict[str, Any], reason: str
) -> None:
    expected = binding()
    store = BindingStore([(expected.external, expected)], [])

    with pytest.raises(IdentityResolutionError) as raised:
        external = teams(**config_changes).external_identity(TeamsActivity.parse(payload))
        await ExternalIdentityResolver(store=store).resolve(external)
    assert raised.value.reason == reason
    assert store.lookups == []


async def test_same_subject_at_a_different_issuer_does_not_match() -> None:
    expected = binding()
    wrong_key = ExternalIdentityKey(
        issuer="https://accounts.google.com",
        subject=AAD_OBJECT_ID,
        tenant=ENTRA_TENANT,
    )
    resolver = ExternalIdentityResolver(store=BindingStore([(wrong_key, expected)], []))

    with pytest.raises(IdentityResolutionError) as raised:
        external = teams().external_identity(TeamsActivity.parse(activity()))
        await resolver.resolve(external)
    assert raised.value.reason == "identity-not-linked"


async def test_same_issuer_and_subject_in_a_different_external_tenant_does_not_match() -> None:
    expected = binding()
    wrong_key = ExternalIdentityKey(
        issuer=ENTRA_ISSUER,
        subject=AAD_OBJECT_ID,
        tenant="different-entra-tenant",
    )
    resolver = ExternalIdentityResolver(store=BindingStore([(wrong_key, expected)], []))

    with pytest.raises(IdentityResolutionError) as raised:
        external = teams().external_identity(TeamsActivity.parse(activity()))
        await resolver.resolve(external)
    assert raised.value.reason == "identity-not-linked"


async def test_two_links_for_one_verified_external_identity_fail_closed() -> None:
    first = binding()
    second_identity = Identity("user-8", roles=frozenset({"acme:viewer"}))
    second = PrincipalBinding(
        identity=second_identity,
        principal=human(second_identity) | member_of("acme", role="viewer"),
        tenant="acme",
        external=first.external,
    )
    resolver = ExternalIdentityResolver(
        store=BindingStore([(first.external, first), (first.external, second)], [])
    )

    with pytest.raises(IdentityResolutionError) as raised:
        external = teams().external_identity(TeamsActivity.parse(activity()))
        await resolver.resolve(external)
    assert raised.value.reason == "ambiguous-identity-link"


async def test_display_name_and_email_changes_cannot_change_the_resolved_principal() -> None:
    expected = binding()
    store = BindingStore([(expected.external, expected)], [])
    resolver = ExternalIdentityResolver(store=store)
    changed_profile = activity(
        **{
            "from": {
                "id": "29:another-channel-id",
                "aadObjectId": AAD_OBJECT_ID,
                "name": "admin@example.test",
                "email": "admin@example.test",
            }
        }
    )

    external = teams().external_identity(TeamsActivity.parse(changed_profile))
    resolved = await resolver.resolve(external)
    assert resolved is expected


async def test_mounted_chatops_carries_one_principal_through_cedar_step_up_and_audit() -> None:
    expected = binding()
    resolver = ExternalIdentityResolver(store=BindingStore([(expected.external, expected)], []))
    decisions: list[tuple[Any, Any]] = []
    audit_records: list[Any] = []

    class Authorizer:
        async def authorize(self, request: Any, requirement: Any) -> AuthorizationDecision:
            decisions.append((request, requirement))
            return AuthorizationDecision(True)

    class Audit:
        async def append(self, record: Any) -> None:
            audit_records.append(record)

    app, chat, _, _ = mounted(
        identity=resolver,
        authorizer=Authorizer(),
        audit=Audit(),
        clock=lambda: 1_800_000_000,
    )
    seen: list[Any] = []

    @chat.command(
        "deploy",
        action="Deploy::run",
        resource=EntityUid("Environment", "production"),
        second_factor=300,
        execution="inline",
    )
    async def deploy(request: Any) -> None:
        seen.append(request)

    response = await post(app, activity())

    assert response.status == 200
    assert len(seen) == 1
    assert seen[0].principal is expected.principal
    assert seen[0].identity == expected.principal.bind()
    assert seen[0].tenant == "acme"
    assert seen[0].identity.roles == frozenset({"acme:operator"})
    assert seen[0].identity.permissions == frozenset({"Deploy::run"})
    assert seen[0].identity.claims["second_factor_at"] == 1_799_999_900
    assert decisions[0][0].principal is expected.principal
    assert decisions[0][1].action == "Deploy::run"
    assert decisions[0][1].resource == EntityUid("Environment", "production")
    assert audit_records[-1].actor.id == "user-7"
    assert audit_records[-1].tenant == "acme"
    assert audit_records[-1].external_identity == expected.external
    assert audit_records[-1].channel_actor_id == "29:opaque-teams-member-id"


async def test_mounted_teams_context_runs_through_the_real_cedar_authorizer() -> None:
    expected = binding()
    resolver = ExternalIdentityResolver(store=BindingStore([(expected.external, expected)], []))
    authorizer = CedarAuthorizer(
        engine=CedarPolicies('permit (principal, action == Action::"Deploy::run", resource);')
    )
    app, chat, _, _ = mounted(identity=resolver, authorizer=authorizer)
    seen: list[str] = []

    @chat.command(
        "deploy",
        action="Deploy::run",
        resource=EntityUid("Environment", "production"),
    )
    async def deploy(request: Any) -> None:
        seen.append(request.identity.id)

    response = await post(app, activity())

    assert response.status == 200
    assert seen == ["user-7"]
