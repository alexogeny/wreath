from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from wreath.chat.teams import (
    BOT_CONNECTOR_ISSUER,
    BOT_CONNECTOR_METADATA_URL,
    Teams,
    TeamsActivity,
    TeamsBotConfig,
    TeamsConnectorVerifier,
    TeamsRefusal,
)

from ._support import APP_ID, ENTRA_TENANT, SERVICE_URL, RecordingFetch, activity

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ROTATED_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(
    claims: dict[str, Any],
    *,
    kid: Any = "connector-key",
    private_key: Any = _PRIVATE_KEY,
    algorithm: str = "RS256",
) -> str:
    header = _b64(json.dumps({"typ": "JWT", "alg": algorithm, "kid": kid}).encode())
    payload = _b64(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


async def _verifier(
    *,
    endorsements: Any = None,
    now: int = 1_750_000_000,
    max_token_lifetime: int = 3600,
) -> TeamsConnectorVerifier:
    numbers = _PRIVATE_KEY.public_key().public_numbers()
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": jwks_url,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            jwks_url: {
                "keys": [
                    {
                        "kid": "connector-key",
                        "kty": "RSA",
                        "alg": "RS256",
                        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
                        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8)),
                        "endorsements": endorsements or ["msteams"],
                    }
                ]
            },
        }
    )
    verifier = TeamsConnectorVerifier(
        config(max_token_lifetime=max_token_lifetime), fetch=fetch, clock=lambda: now
    )
    await verifier.startup()
    return verifier


def config(**changes: Any) -> TeamsBotConfig:
    values = {
        "app_id": APP_ID,
        "app_secret": "not-used-for-inbound-verification",
        "messaging_endpoint": "https://chat.example.test/teams/activities",
        "allowed_tenants": frozenset({ENTRA_TENANT}),
    }
    values.update(changes)
    return TeamsBotConfig(**values)


def test_default_provider_constructs_its_zero_dependency_protocol_adapters() -> None:
    teams = Teams(config=config())
    assert isinstance(teams.verifier, TeamsConnectorVerifier)
    assert callable(teams.connector.send)


def test_activity_parses_the_protocol_identity_and_routing_fields() -> None:
    parsed = TeamsActivity.parse(activity())

    assert parsed.id == "activity-1"
    assert parsed.kind == "message"
    assert parsed.channel == "msteams"
    assert parsed.service_url == SERVICE_URL
    assert parsed.tenant_id == ENTRA_TENANT
    assert parsed.aad_object_id == "22222222-2222-4222-8222-222222222222"
    assert parsed.installation_key == (ENTRA_TENANT, "19:team@thread.tacv2")
    assert parsed.conversation_id == "19:conversation@thread.tacv2"
    assert parsed.reply_to_id == "activity-1"
    assert parsed.sender_id == "29:opaque-teams-member-id"
    assert parsed.recipient_id == APP_ID
    assert parsed.recipient_name == "Wreath"


@pytest.mark.parametrize("conversation_type", ["personal", "groupChat"])
def test_non_channel_conversations_use_the_conversation_as_the_installation(
    conversation_type: str,
) -> None:
    payload = activity(
        conversation={
            "id": "19:chat@unq.gbl.spaces",
            "conversationType": conversation_type,
            "tenantId": ENTRA_TENANT,
        },
        channelData={"tenant": {"id": ENTRA_TENANT}},
    )

    parsed = TeamsActivity.parse(payload)
    assert parsed.installation_key == (ENTRA_TENANT, "19:chat@unq.gbl.spaces")


@pytest.mark.parametrize(
    ("payload", "reason", "message"),
    [
        (activity(id=None), "missing-activity-id", "activity id"),
        (activity(serviceUrl=None), "missing-service-url", "serviceUrl"),
        (activity(channelId="webchat"), "wrong-channel", "msteams"),
        (activity(conversation={"id": "c"}), "missing-tenant", "tenant"),
        (activity(channelData={"tenant": {"id": ENTRA_TENANT}}), "missing-installation", "team"),
    ],
)
def test_malformed_or_non_teams_activities_are_refused_distinctly(
    payload: dict[str, Any], reason: str, message: str
) -> None:
    with pytest.raises(TeamsRefusal, match=message) as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == reason


def test_conflicting_tenant_locations_are_refused_instead_of_preferred() -> None:
    payload = activity()
    payload["conversation"]["tenantId"] = "different-tenant"

    with pytest.raises(TeamsRefusal, match="two different tenants") as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == "ambiguous-tenant"


def test_activity_rejects_a_non_object_payload() -> None:
    payload: Any = []
    with pytest.raises(TeamsRefusal, match="JSON object") as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == "malformed-activity"


@pytest.mark.parametrize("conversation_type", [None, "", 7])
def test_missing_or_invalid_conversation_type_uses_channel_semantics(
    conversation_type: Any,
) -> None:
    payload = activity()
    payload["conversation"]["conversationType"] = conversation_type
    assert TeamsActivity.parse(payload).conversation_type == "channel"


@pytest.mark.parametrize("tenant", [None, "", 7])
def test_channel_data_never_supplies_an_invalid_tenant(tenant: Any) -> None:
    payload = activity(channelData={"tenant": {"id": tenant}, "team": {"id": "team"}})
    parsed = TeamsActivity.parse(payload)
    assert parsed.tenant_id == ENTRA_TENANT
    assert parsed.installation_key == (ENTRA_TENANT, "team")


@pytest.mark.parametrize("tenant", [None, "", 7])
def test_conversation_must_supply_a_nonempty_string_tenant(tenant: Any) -> None:
    payload = activity()
    payload["conversation"]["tenantId"] = tenant
    with pytest.raises(TeamsRefusal) as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == "missing-tenant"


@pytest.mark.parametrize("team_id", [None, "", 7])
def test_channel_installation_id_must_be_nonempty_text(team_id: Any) -> None:
    payload = activity()
    payload["channelData"]["team"]["id"] = team_id
    with pytest.raises(TeamsRefusal) as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == "missing-installation"


def test_optional_activity_fields_are_typed_at_the_protocol_boundary() -> None:
    payload = activity(
        type=7,
        text=7,
        name=7,
        action=7,
        membersAdded=[None, {"id": 1}, {"id": "member"}],
    )
    payload["from"] = {"id": 7, "aadObjectId": 7, "name": 7}
    payload["recipient"] = {"id": 7, "name": 7}

    parsed = TeamsActivity.parse(payload)
    assert parsed.kind == ""
    assert parsed.sender_id == ""
    assert parsed.recipient_id == ""
    assert parsed.aad_object_id is None
    assert parsed.sender_name is None
    assert parsed.recipient_name is None
    assert parsed.text is None
    assert parsed.name is None
    assert parsed.action is None
    assert parsed.members_added == ("member",)
    assert TeamsActivity.parse(activity(membersAdded={"id": "member"})).members_added == ()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("id", "", "missing-activity-id"),
        ("id", 7, "missing-activity-id"),
        ("serviceUrl", "", "missing-service-url"),
        ("serviceUrl", 7, "missing-service-url"),
    ],
)
def test_required_top_level_activity_text_is_nonempty_and_typed(
    field: str, value: Any, reason: str
) -> None:
    payload = activity()
    payload[field] = value
    with pytest.raises(TeamsRefusal) as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == reason


@pytest.mark.parametrize("value", ["", 7])
def test_conversation_id_is_nonempty_text(value: Any) -> None:
    payload = activity()
    payload["conversation"]["id"] = value
    with pytest.raises(TeamsRefusal) as raised:
        TeamsActivity.parse(payload)
    assert raised.value.reason == "missing-conversation"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"app_id": ""}, "app_id"),
        ({"app_secret": ""}, "app_secret"),
        ({"messaging_endpoint": "http://chat.example.test/teams"}, "HTTPS"),
        ({"allowed_tenants": frozenset()}, "allowed_tenants"),
    ],
)
def test_incomplete_or_unsafe_configuration_refuses_at_construction(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        config(**changes)


def test_allowed_tenants_cannot_contain_an_empty_identifier() -> None:
    with pytest.raises(ValueError, match="empty tenant"):
        config(allowed_tenants=frozenset({ENTRA_TENANT, ""}))


def test_login_issuer_must_belong_to_an_allowed_tenant() -> None:
    with pytest.raises(ValueError, match="not present in allowed_tenants"):
        config(login_issuers={"another-tenant": "https://login.microsoftonline.com/common/v2.0"})


@pytest.mark.parametrize("lifetime", [0, -1, 86_401])
def test_token_lifetime_bound_must_be_positive_and_bounded(lifetime: int) -> None:
    with pytest.raises(ValueError, match="max_token_lifetime"):
        config(max_token_lifetime=lifetime)


def test_https_configuration_rejects_each_authority_and_suffix_hazard() -> None:
    for url in (
        "https://user@chat.example.test/path",
        "https://:password@chat.example.test/path",
        "https://chat.example.test/path?query=1",
        "https://chat.example.test/path#fragment",
    ):
        with pytest.raises(ValueError, match="HTTPS"):
            config(messaging_endpoint=url)


async def test_startup_fetches_bot_connector_metadata_and_keys_once() -> None:
    numbers = _PRIVATE_KEY.public_key().public_numbers()
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": jwks_url,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            jwks_url: {
                "keys": [
                    {
                        "kid": "connector-key",
                        "kty": "RSA",
                        "alg": "RS256",
                        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
                        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8)),
                        "endorsements": ["msteams"],
                    }
                ]
            },
        }
    )
    verifier = TeamsConnectorVerifier(config(), fetch=fetch)

    assert await verifier.startup() == 1
    assert fetch.calls == [BOT_CONNECTOR_METADATA_URL, jwks_url]
    assert verifier.fetches_on_request_path is True


async def test_startup_refuses_metadata_that_moves_jwks_off_the_trusted_origin() -> None:
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "http://127.0.0.1/internal",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        }
    )

    with pytest.raises(TeamsRefusal, match="JWKS") as raised:
        await TeamsConnectorVerifier(config(), fetch=fetch).startup()
    assert raised.value.reason == "unsafe-jwks-uri"


async def test_startup_rejects_each_jwks_origin_authority_hazard() -> None:
    for jwks_uri in (
        "https:///v1/keys",
        "https://login.botframework.com:444/v1/keys",
        "https://user@login.botframework.com/v1/keys",
        "https://:password@login.botframework.com/v1/keys",
    ):
        fetch = RecordingFetch(
            {
                BOT_CONNECTOR_METADATA_URL: {
                    "issuer": BOT_CONNECTOR_ISSUER,
                    "jwks_uri": jwks_uri,
                    "id_token_signing_alg_values_supported": ["RS256"],
                }
            }
        )
        with pytest.raises(TeamsRefusal) as raised:
            await TeamsConnectorVerifier(config(), fetch=fetch).startup()
        assert raised.value.reason == "unsafe-jwks-uri"


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (
            {
                "issuer": "https://attacker.example",
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            "wrong-metadata-issuer",
        ),
        (
            {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": "RS256",
            },
            "unsupported-signing-algorithm",
        ),
        (
            {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": ["ES256"],
            },
            "unsupported-signing-algorithm",
        ),
        (
            {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": 7,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            "unsafe-jwks-uri",
        ),
    ],
)
async def test_startup_refuses_untrusted_connector_metadata(
    metadata: dict[str, Any], reason: str
) -> None:
    fetch = RecordingFetch({BOT_CONNECTOR_METADATA_URL: metadata})
    with pytest.raises(TeamsRefusal) as raised:
        await TeamsConnectorVerifier(config(), fetch=fetch).startup()
    assert raised.value.reason == reason


async def test_startup_refuses_string_instead_of_algorithm_list() -> None:
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": "RS256",
            }
        }
    )
    with pytest.raises(TeamsRefusal) as raised:
        await TeamsConnectorVerifier(config(), fetch=fetch).startup()
    assert raised.value.reason == "unsupported-signing-algorithm"


async def test_startup_refuses_a_non_list_jwks() -> None:
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": jwks_url,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            jwks_url: {"keys": {}},
        }
    )
    with pytest.raises(TeamsRefusal) as raised:
        await TeamsConnectorVerifier(config(), fetch=fetch).startup()
    assert raised.value.reason == "malformed-jwks"


async def test_verifier_refuses_key_refresh_before_startup_metadata() -> None:
    verifier = TeamsConnectorVerifier(config(), fetch=RecordingFetch({}))
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_100,
    }
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims)}", activity())
    assert raised.value.reason == "missing-jwks-uri"


async def test_startup_ignores_every_non_rsa_or_ambiguous_jwk_entry() -> None:
    numbers = _PRIVATE_KEY.public_key().public_numbers()
    modulus = _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8))
    exponent = _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8))
    rotated_modulus = _b64(_ROTATED_PRIVATE_KEY.public_key().public_numbers().n.to_bytes(256))
    usable = {
        "kid": "connector-key",
        "kty": "RSA",
        "alg": "RS256",
        "n": modulus,
        "e": exponent,
        "endorsements": [7, "msteams"],
    }
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": jwks_url,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            jwks_url: {
                "keys": [
                    None,
                    {**usable, "kid": 7},
                    {**usable, "kid": ""},
                    {**usable, "kid": "ec", "kty": "EC"},
                    {**usable, "kid": "hs", "alg": "HS256"},
                    usable,
                    {**usable, "n": rotated_modulus},
                ]
            },
        }
    )
    verifier = TeamsConnectorVerifier(config(), fetch=fetch, clock=lambda: 1_750_000_000)
    assert await verifier.startup() == 1
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    await verifier.verify(f"Bearer {_token(claims)}", activity())


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"iss": "https://attacker.example"}, "wrong-issuer"),
        ({"aud": "another-app"}, "wrong-audience"),
        ({"aud": [APP_ID, 7]}, "wrong-audience"),
        ({"aud": 7}, "wrong-audience"),
        ({"serviceurl": "https://smba.trafficmanager.net/emea/"}, "wrong-service-url"),
        ({"exp": 1_700_000_000}, "expired-token"),
        ({"exp": "1750000100"}, "expired-token"),
        ({"nbf": 1_800_000_000}, "token-not-yet-valid"),
    ],
)
async def test_verified_signature_is_not_enough_when_registered_claims_disagree(
    claims: dict[str, Any], reason: str
) -> None:
    valid = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    valid.update(claims)
    verifier = await _verifier(endorsements=[[], "msteams"])

    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(valid)}", activity())
    assert raised.value.reason == reason


async def test_verified_token_requires_rs256_even_with_a_valid_rsa_signature() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    verifier = await _verifier(endorsements=[[], "msteams"])
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims, algorithm='HS256')}", activity())
    assert raised.value.reason == "unsupported-signing-algorithm"


async def test_verified_token_rejects_a_signature_from_another_key() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    verifier = await _verifier()
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(
            f"Bearer {_token(claims, private_key=_ROTATED_PRIVATE_KEY)}", activity()
        )
    assert raised.value.reason == "invalid-signature"


async def test_channel_id_must_be_text_even_when_a_key_has_teams_endorsement() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    payload = activity()
    payload["channelId"] = 7
    verifier = await _verifier(endorsements=[[], "msteams"])
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims)}", payload)
    assert raised.value.reason == "missing-channel-endorsement"


async def test_unhashable_channel_and_key_identifiers_are_authentication_refusals() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_100,
    }
    payload = activity()
    payload["channelId"] = []
    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier()).verify(f"Bearer {_token(claims)}", payload)
    assert raised.value.reason == "missing-channel-endorsement"

    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier()).verify(f"Bearer {_token(claims, kid=[])}", activity())
    assert raised.value.reason == "unknown-signing-key"


async def test_mixed_type_audience_is_never_partially_accepted() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": [APP_ID, 7],
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_100,
    }
    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier()).verify(f"Bearer {_token(claims)}", activity())
    assert raised.value.reason == "wrong-audience"


async def test_expired_token_never_reaches_channel_authorization() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_700_000_000,
    }
    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier(endorsements=[])).verify(f"Bearer {_token(claims)}", activity())
    assert raised.value.reason == "expired-token"


async def test_token_cannot_outlive_the_configured_replay_window() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_901,
    }
    verifier = await _verifier(max_token_lifetime=600)
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims)}", activity())
    assert raised.value.reason == "token-lifetime-too-long"


async def test_token_accepts_a_string_audience_list_and_optional_not_before() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": ["another-audience", APP_ID],
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_100,
    }
    await (await _verifier()).verify(f"Bearer {_token(claims)}", activity())


async def test_token_key_id_must_be_text() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_100,
    }
    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier()).verify(f"Bearer {_token(claims, kid=7)}", activity())
    assert raised.value.reason == "unknown-signing-key"


async def test_missing_teams_endorsement_is_a_forbidden_request() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    verifier = await _verifier(endorsements=["webchat"])

    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims)}", activity())
    assert raised.value.reason == "missing-channel-endorsement"
    assert raised.value.status == 403


async def test_non_list_endorsements_do_not_authorize_a_channel() -> None:
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_000_100,
    }
    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier(endorsements=("msteams",))).verify(
            f"Bearer {_token(claims)}", activity()
        )
    assert raised.value.reason == "missing-channel-endorsement"


@pytest.mark.parametrize(
    "header", [None, "", "Basic abc", "Bearer", "Bearer malformed", "Bearer a.b.c.d"]
)
async def test_missing_or_malformed_authorization_never_reaches_activity_handling(
    header: str | None,
) -> None:
    verifier = await _verifier()
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(header, activity())
    assert raised.value.status == 401


async def test_non_bearer_authorization_is_not_reclassified_as_a_malformed_jwt() -> None:
    with pytest.raises(TeamsRefusal) as raised:
        await (await _verifier()).verify("Basic a.b.c", activity())
    assert raised.value.reason == "missing-authorization"


async def test_unknown_key_triggers_at_most_one_bounded_refresh() -> None:
    now = 1_750_000_000
    verifier = await _verifier(now=now)
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }
    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims, kid='unknown')}", activity())
    assert raised.value.reason == "unknown-signing-key"
    assert verifier.refresh_count == 0


async def test_unknown_key_refreshes_jwks_once_after_the_bounded_interval() -> None:
    now = [1_750_000_000.0]
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    initial = _PRIVATE_KEY.public_key().public_numbers()
    rotated = _ROTATED_PRIVATE_KEY.public_key().public_numbers()
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": jwks_url,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            jwks_url: {
                "keys": [
                    {
                        "kid": "connector-key",
                        "kty": "RSA",
                        "alg": "RS256",
                        "n": _b64(initial.n.to_bytes((initial.n.bit_length() + 7) // 8)),
                        "e": _b64(initial.e.to_bytes((initial.e.bit_length() + 7) // 8)),
                        "endorsements": ["msteams"],
                    }
                ]
            },
        }
    )
    verifier = TeamsConnectorVerifier(config(), fetch=fetch, clock=lambda: now[0])
    await verifier.startup()
    fetch.responses[jwks_url] = {
        "keys": [
            {
                "kid": "rotated-key",
                "kty": "RSA",
                "alg": "RS256",
                "n": _b64(rotated.n.to_bytes((rotated.n.bit_length() + 7) // 8)),
                "e": _b64(rotated.e.to_bytes((rotated.e.bit_length() + 7) // 8)),
                "endorsements": ["msteams"],
            }
        ]
    }
    now[0] += 301
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_001_000,
    }

    await verifier.verify(
        f"Bearer {_token(claims, kid='rotated-key', private_key=_ROTATED_PRIVATE_KEY)}",
        activity(),
    )

    assert verifier.refresh_count == 1
    assert fetch.calls == [BOT_CONNECTOR_METADATA_URL, jwks_url, jwks_url]


async def test_concurrent_unknown_key_requests_share_one_bounded_refresh() -> None:
    now = [1_750_000_000.0]
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    initial = _PRIVATE_KEY.public_key().public_numbers()
    rotated = _ROTATED_PRIVATE_KEY.public_key().public_numbers()
    responses = {
        BOT_CONNECTOR_METADATA_URL: {
            "issuer": BOT_CONNECTOR_ISSUER,
            "jwks_uri": jwks_url,
            "id_token_signing_alg_values_supported": ["RS256"],
        },
        jwks_url: {
            "keys": [
                {
                    "kid": "connector-key",
                    "kty": "RSA",
                    "alg": "RS256",
                    "n": _b64(initial.n.to_bytes((initial.n.bit_length() + 7) // 8)),
                    "e": _b64(initial.e.to_bytes((initial.e.bit_length() + 7) // 8)),
                    "endorsements": ["msteams"],
                }
            ]
        },
    }
    calls: list[str] = []

    async def fetch(url: str) -> Any:
        calls.append(url)
        await asyncio.sleep(0)
        return responses[url]

    verifier = TeamsConnectorVerifier(config(), fetch=fetch, clock=lambda: now[0])
    await verifier.startup()
    responses[jwks_url] = {
        "keys": [
            {
                "kid": "rotated-key",
                "kty": "RSA",
                "alg": "RS256",
                "n": _b64(rotated.n.to_bytes((rotated.n.bit_length() + 7) // 8)),
                "e": _b64(rotated.e.to_bytes((rotated.e.bit_length() + 7) // 8)),
                "endorsements": ["msteams"],
            }
        ]
    }
    now[0] += 301
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_001_000,
    }
    token = f"Bearer {_token(claims, kid='rotated-key', private_key=_ROTATED_PRIVATE_KEY)}"
    await asyncio.gather(
        verifier.verify(token, activity(id="one")),
        verifier.verify(token, activity(id="two")),
    )
    assert verifier.refresh_count == 1
    assert calls == [BOT_CONNECTOR_METADATA_URL, jwks_url, jwks_url]


async def test_known_key_never_refreshes_only_because_the_interval_elapsed() -> None:
    now = [1_750_000_000.0]
    verifier = await _verifier(now=int(now[0]))
    verifier._clock = lambda: now[0]
    now[0] += 301
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "exp": 1_750_001_000,
    }
    await verifier.verify(f"Bearer {_token(claims)}", activity())
    assert verifier.refresh_count == 0


async def test_malformed_audience_is_an_authentication_refusal() -> None:
    verifier = await _verifier()
    claims = {
        "iss": BOT_CONNECTOR_ISSUER,
        "aud": 42,
        "serviceurl": SERVICE_URL,
        "nbf": 1_749_999_900,
        "exp": 1_750_000_100,
    }

    with pytest.raises(TeamsRefusal) as raised:
        await verifier.verify(f"Bearer {_token(claims)}", activity())

    assert raised.value.reason == "wrong-audience"


async def test_startup_refuses_a_weak_rsa_key_instead_of_falling_back() -> None:
    jwks_url = "https://login.botframework.com/v1/.well-known/keys"
    fetch = RecordingFetch(
        {
            BOT_CONNECTOR_METADATA_URL: {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": jwks_url,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            jwks_url: {
                "keys": [
                    {
                        "kid": "weak",
                        "kty": "RSA",
                        "alg": "RS256",
                        "n": "AQAB",
                        "e": "AQAB",
                        "endorsements": ["msteams"],
                    }
                ]
            },
        }
    )

    with pytest.raises(TeamsRefusal) as raised:
        await TeamsConnectorVerifier(config(), fetch=fetch).startup()

    assert raised.value.reason == "empty-jwks"
