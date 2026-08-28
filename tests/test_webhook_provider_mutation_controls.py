"""Focused objections for third-party webhook verification profiles."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

import wreath.webhooks as webhook_module
from wreath._json import dumps
from wreath.webhooks import (
    GitHubWebhookVerifier,
    HMACWebhookSigner,
    HMACWebhookVerifier,
    StandardWebhookVerifier,
    StripeWebhookVerifier,
    WebhookEnvelope,
    _constant_time_signature_match,
    _parse_timestamp,
    _provider_event,
    _signature_base,
    _unix_timestamp,
)

NOW = datetime(2026, 8, 25, 3, 4, 5, tzinfo=UTC)
SECONDS = str(int(NOW.timestamp())).encode("ascii")
BODY = dumps({"id": "evt_1", "type": "invoice.paid", "api_version": "2026-08"})


def test_hmac_verifier_refuses_delete_in_a_signed_field_before_mac_check() -> None:
    secret = b"s" * 32
    envelope = WebhookEnvelope(
        "evt_1", "invoice.paid", "1", NOW, "application/json", BODY
    )
    headers = dict(HMACWebhookSigner({"key": secret}, key_id="key").headers(envelope))
    headers[b"wreath-webhook-id"] = b"evt_1\x7f"

    with pytest.raises(ValueError, match="id contains a control character"):
        HMACWebhookVerifier({"key": secret}).verify(
            body=BODY, headers=headers, now=NOW
        )


def test_provider_event_requires_a_json_object() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        _provider_event(b"[]")


def test_provider_event_requires_a_string_id() -> None:
    with pytest.raises(ValueError, match="no string id"):
        _provider_event(dumps({"id": 7, "type": "created"}))


def test_provider_event_requires_a_nonempty_id() -> None:
    with pytest.raises(ValueError, match="no string id"):
        _provider_event(dumps({"id": "", "type": "created"}))


def test_provider_event_requires_a_string_type() -> None:
    with pytest.raises(ValueError, match="no string type"):
        _provider_event(dumps({"id": "evt", "type": 7}))


def test_provider_event_requires_a_nonempty_type() -> None:
    with pytest.raises(ValueError, match="no string type"):
        _provider_event(dumps({"id": "evt", "type": ""}))


def test_provider_event_preserves_string_versions() -> None:
    assert _provider_event(dumps({"id": "evt", "type": "created", "version": "v2"})) == (
        "evt",
        "created",
        "v2",
    )


def test_provider_event_stringifies_nonstring_versions() -> None:
    assert _provider_event(dumps({"id": "evt", "type": "created", "version": 2})) == (
        "evt",
        "created",
        "2",
    )


def test_unix_timestamp_accepts_an_explicit_aware_now() -> None:
    assert _unix_timestamp(SECONDS, NOW.astimezone(timezone(timedelta(hours=10))), 1) == NOW


def test_unix_timestamp_uses_the_current_clock_when_now_is_absent() -> None:
    seconds = int(time.time())

    parsed = _unix_timestamp(str(seconds).encode("ascii"), None, 5)

    assert abs(parsed.timestamp() - seconds) < 1


def test_unix_timestamp_refuses_a_value_outside_the_window() -> None:
    stale = str(int((NOW - timedelta(seconds=11)).timestamp())).encode("ascii")

    with pytest.raises(ValueError, match="outside the accepted window"):
        _unix_timestamp(stale, NOW, 10)


def test_constant_time_signature_match_distinguishes_match_and_miss() -> None:
    assert _constant_time_signature_match((b"old", b"wanted"), (b"bad", b"wanted"))
    assert not _constant_time_signature_match((b"wanted",), (b"bad",))


def test_constant_time_signature_match_checks_bytes_after_a_digest_collision(
    monkeypatch,
) -> None:
    class _CollidingDigest:
        def digest(self) -> bytes:
            return b"same bucket"

    monkeypatch.setattr(
        webhook_module.hashlib, "sha256", lambda _value: _CollidingDigest()
    )

    assert not _constant_time_signature_match((b"wanted",), (b"different",))


def _standard_headers(
    secret: bytes, *, body: bytes = BODY, event_id: bytes = b"evt_1"
) -> dict[bytes, bytes]:
    signed = event_id + b"." + SECONDS + b"." + body
    signature = base64.b64encode(hmac.digest(secret, signed, "sha256"))
    return {
        b"webhook-id": event_id,
        b"webhook-timestamp": SECONDS,
        b"webhook-signature": b"v1," + signature,
    }


def test_standard_verifier_decodes_string_secrets() -> None:
    encoded = "whsec_" + base64.b64encode(b"secret").decode("ascii")

    result = StandardWebhookVerifier(encoded).verify(
        body=BODY, headers=_standard_headers(b"secret"), now=NOW
    )

    assert result.id == "evt_1"


def test_standard_verifier_accepts_raw_byte_secrets() -> None:
    result = StandardWebhookVerifier(b"secret").verify(
        body=BODY, headers=_standard_headers(b"secret"), now=NOW
    )

    assert result.id == "evt_1"


def test_standard_verifier_refuses_a_decoded_empty_secret() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        StandardWebhookVerifier("whsec_")


def test_standard_verifier_refuses_nonpositive_max_age() -> None:
    with pytest.raises(ValueError, match="max_age must be positive"):
        StandardWebhookVerifier(b"secret", max_age=0)


def test_standard_verifier_ignores_other_signature_versions() -> None:
    headers = _standard_headers(b"secret")
    headers[b"webhook-signature"] = b"v0,%%% " + headers[b"webhook-signature"]

    assert StandardWebhookVerifier(b"secret").verify(
        body=BODY, headers=headers, now=NOW
    ).id == "evt_1"


def test_standard_verifier_refuses_a_header_without_v1_signatures() -> None:
    headers = _standard_headers(b"secret")
    headers[b"webhook-signature"] = b"v0,AAAA"

    with pytest.raises(ValueError, match="invalid Standard Webhooks signature"):
        StandardWebhookVerifier(b"secret").verify(
            body=BODY, headers=headers, now=NOW
        )


def test_standard_verifier_refuses_a_malformed_v1_signature() -> None:
    headers = _standard_headers(b"secret")
    headers[b"webhook-signature"] = b"v1,%%%"

    with pytest.raises(ValueError, match="invalid Standard Webhooks signature"):
        StandardWebhookVerifier(b"secret").verify(
            body=BODY, headers=headers, now=NOW
        )


def test_standard_verifier_binds_header_id_to_body_id() -> None:
    body = dumps({"id": "body-id", "type": "created"})

    with pytest.raises(ValueError, match="body id differs"):
        StandardWebhookVerifier(b"secret").verify(
            body=body,
            headers=_standard_headers(b"secret", body=body, event_id=b"header-id"),
            now=NOW,
        )


def _stripe_headers(
    secret: bytes, *, body: bytes = BODY, timestamp: bytes = SECONDS
) -> dict[bytes, bytes]:
    signature = hmac.new(
        secret, timestamp + b"." + body, hashlib.sha256
    ).hexdigest().encode("ascii")
    return {b"stripe-signature": b"t=" + timestamp + b",v1=" + signature}


def test_stripe_verifier_refuses_an_empty_secret_collection() -> None:
    with pytest.raises(ValueError, match="at least one non-empty"):
        StripeWebhookVerifier(())


def test_stripe_verifier_refuses_an_empty_secret_within_rotation() -> None:
    with pytest.raises(ValueError, match="at least one non-empty"):
        StripeWebhookVerifier((b"secret", b""))


def test_stripe_verifier_refuses_nonpositive_max_age() -> None:
    with pytest.raises(ValueError, match="max_age must be positive"):
        StripeWebhookVerifier(b"secret", max_age=0)


def test_stripe_verifier_accepts_string_and_byte_secrets() -> None:
    headers = _stripe_headers(b"secret")

    assert StripeWebhookVerifier("secret").verify(
        body=BODY, headers=headers, now=NOW
    ).id == "evt_1"
    assert StripeWebhookVerifier(b"secret").verify(
        body=BODY, headers=headers, now=NOW
    ).id == "evt_1"


def test_stripe_verifier_requires_equals_in_each_field() -> None:
    with pytest.raises(ValueError, match="invalid Stripe-Signature field"):
        StripeWebhookVerifier(b"secret").verify(
            body=BODY, headers={b"stripe-signature": b"t"}, now=NOW
        )


def test_stripe_verifier_requires_exactly_one_timestamp() -> None:
    headers = _stripe_headers(b"secret")
    headers[b"stripe-signature"] += b",t=" + SECONDS

    with pytest.raises(ValueError, match="needs one t"):
        StripeWebhookVerifier(b"secret").verify(body=BODY, headers=headers, now=NOW)


def test_stripe_verifier_requires_a_v1_signature() -> None:
    with pytest.raises(ValueError, match="at least one v1"):
        StripeWebhookVerifier(b"secret").verify(
            body=BODY,
            headers={b"stripe-signature": b"t=" + SECONDS},
            now=NOW,
        )


def test_github_verifier_refuses_an_empty_secret() -> None:
    with pytest.raises(ValueError, match="secret cannot be empty"):
        GitHubWebhookVerifier(b"")


def test_github_verifier_refuses_nonpositive_replay_ttl() -> None:
    with pytest.raises(ValueError, match="replay_ttl must be positive"):
        GitHubWebhookVerifier(b"secret", replay_ttl=0)


def test_github_verifier_accepts_string_and_byte_secrets() -> None:
    signature = b"sha256=" + hmac.new(
        b"secret", BODY, hashlib.sha256
    ).hexdigest().encode("ascii")
    headers = {
        b"x-hub-signature-256": signature,
        b"x-github-delivery": b"delivery",
        b"x-github-event": b"push",
    }

    assert GitHubWebhookVerifier("secret").verify(
        body=BODY, headers=headers, now=NOW
    ).timestamp == NOW
    assert GitHubWebhookVerifier(b"secret").verify(
        body=BODY, headers=headers, now=NOW
    ).id == "delivery"


def test_github_verifier_refuses_an_invalid_signature() -> None:
    with pytest.raises(ValueError, match="invalid GitHub webhook signature"):
        GitHubWebhookVerifier(b"secret").verify(
            body=BODY,
            headers={b"x-hub-signature-256": b"sha256=bad"},
            now=NOW,
        )


def test_github_verifier_uses_current_clock_when_now_is_absent() -> None:
    signature = b"sha256=" + hmac.new(
        b"secret", BODY, hashlib.sha256
    ).hexdigest().encode("ascii")
    before = datetime.now(UTC)

    result = GitHubWebhookVerifier(b"secret").verify(
        body=BODY,
        headers={
            b"x-hub-signature-256": signature,
            b"x-github-delivery": b"delivery",
            b"x-github-event": b"push",
        },
    )

    assert before <= result.timestamp <= datetime.now(UTC)


def test_parse_timestamp_accepts_z_and_offset_forms() -> None:
    assert _parse_timestamp(b"2026-08-25T03:04:05Z") == NOW
    assert _parse_timestamp(b"2026-08-25T13:04:05+10:00") == NOW


def test_parse_timestamp_refuses_a_naive_value() -> None:
    with pytest.raises(ValueError, match="must include a timezone"):
        _parse_timestamp(b"2026-08-25T03:04:05")


def test_signature_base_distinguishes_plain_and_relay_profiles() -> None:
    plain = _signature_base(SECONDS, "evt", "created", b"body")
    relayed = _signature_base(SECONDS, "evt", "created", b"body", ("api",))

    assert plain.startswith(b"wreath-v1\n")
    assert relayed.startswith(b"wreath-v1-relay\n")
    assert b"\napi\nbody" in relayed
