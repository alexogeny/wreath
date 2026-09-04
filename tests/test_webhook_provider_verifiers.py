from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime

import pytest

import wreath.webhooks as webhook_module
from wreath._json import dumps
from wreath.webhooks import (
    GitHubWebhookVerifier,
    StandardWebhookVerifier,
    StripeWebhookVerifier,
    WebhookVerifier,
)

NOW = datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)
SECONDS = str(int(NOW.timestamp())).encode("ascii")
BODY = dumps({"id": "evt_1", "type": "invoice.paid", "api_version": "2026-08"})


@pytest.mark.parametrize(
    "build",
    [
        lambda: StandardWebhookVerifier(32),
        lambda: StripeWebhookVerifier(32),
        lambda: GitHubWebhookVerifier(32),
    ],
)
def test_provider_verifiers_refuse_non_text_and_non_bytes_secrets(build) -> None:
    with pytest.raises(TypeError, match="webhook secret must be bytes or str"):
        build()


@pytest.mark.parametrize("window", [float("nan"), float("inf")])
def test_provider_freshness_and_replay_windows_must_be_finite(window: float) -> None:
    for build in (
        lambda: StandardWebhookVerifier(b"secret", max_age=window),
        lambda: StripeWebhookVerifier(b"secret", max_age=window),
        lambda: GitHubWebhookVerifier(b"secret", replay_ttl=window),
    ):
        with pytest.raises(ValueError, match="positive and finite"):
            build()


@pytest.mark.parametrize(
    "verifier",
    [
        StandardWebhookVerifier(b"secret"),
        StripeWebhookVerifier(b"secret"),
        GitHubWebhookVerifier(b"secret"),
    ],
)
def test_provider_verifier_replay_windows_are_immutable(verifier) -> None:
    with pytest.raises(AttributeError):
        verifier.max_age = 3600


def test_standard_webhooks_profile() -> None:
    secret = b"standard-secret"
    signature = base64.b64encode(hmac.digest(secret, b"evt_1." + SECONDS + b"." + BODY, "sha256"))
    verifier = StandardWebhookVerifier(secret)
    assert isinstance(verifier, WebhookVerifier)
    result = verifier.verify(
        body=BODY,
        headers={
            b"Webhook-Id": b"evt_1",
            b"Webhook-Timestamp": SECONDS,
            b"Webhook-Signature": b"v1," + signature,
        },
        now=NOW,
    )
    assert (result.id, result.type, result.version) == ("evt_1", "invoice.paid", "2026-08")


def test_stripe_profile() -> None:
    secret = b"whsec_test"
    signature = hmac.new(secret, SECONDS + b"." + BODY, hashlib.sha256).hexdigest().encode("ascii")
    result = StripeWebhookVerifier(secret).verify(
        body=BODY,
        headers={b"Stripe-Signature": b"t=" + SECONDS + b",v1=" + signature},
        now=NOW,
    )
    assert result.type == "invoice.paid"


def test_standard_signature_rotation_hashes_each_secret_once(monkeypatch) -> None:
    secrets = tuple(f"secret-{index}".encode() for index in range(8))
    signed = b"evt_1." + SECONDS + b"." + BODY
    wanted = hmac.digest(secrets[-1], signed, "sha256")
    signatures = [bytes([index]) * 32 for index in range(32)] + [wanted]
    header = b" ".join(b"v1," + base64.b64encode(value) for value in signatures)
    original = webhook_module.hmac.digest
    calls = 0

    def counted_digest(key, message, digest):
        nonlocal calls
        calls += 1
        return original(key, message, digest)

    monkeypatch.setattr(webhook_module.hmac, "digest", counted_digest)
    StandardWebhookVerifier(secrets).verify(
        body=BODY,
        headers={
            b"webhook-id": b"evt_1",
            b"webhook-timestamp": SECONDS,
            b"webhook-signature": header,
        },
        now=NOW,
    )
    assert calls == len(secrets)


def test_standard_profile_refuses_non_v1_signatures_before_computing_a_mac(
    monkeypatch,
) -> None:
    original = webhook_module.hmac.digest
    calls = 0

    def counted_digest(key, message, digest):
        nonlocal calls
        calls += 1
        return original(key, message, digest)

    monkeypatch.setattr(webhook_module.hmac, "digest", counted_digest)
    with pytest.raises(ValueError, match="invalid Standard Webhooks signature"):
        StandardWebhookVerifier(b"standard-secret").verify(
            body=BODY,
            headers={
                b"webhook-id": b"evt_1",
                b"webhook-timestamp": SECONDS,
                b"webhook-signature": b"v2,AAAA",
            },
            now=NOW,
        )
    assert calls == 0


def test_standard_profile_refuses_a_malformed_v1_signature() -> None:
    with pytest.raises(ValueError, match="invalid Standard Webhooks signature") as exc_info:
        StandardWebhookVerifier(b"standard-secret").verify(
            body=BODY,
            headers={
                b"webhook-id": b"evt_1",
                b"webhook-timestamp": SECONDS,
                b"webhook-signature": b"v1,not-base64!",
            },
            now=NOW,
        )
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_stripe_signature_rotation_hashes_each_secret_once(monkeypatch) -> None:
    secrets = tuple(f"secret-{index}".encode() for index in range(8))
    signed = SECONDS + b"." + BODY
    wanted = hmac.new(secrets[-1], signed, hashlib.sha256).hexdigest().encode()
    signatures = [f"{index:064x}".encode() for index in range(32)] + [wanted]
    header = b"t=" + SECONDS + b"," + b",".join(b"v1=" + value for value in signatures)
    original = webhook_module.hmac.new
    calls = 0

    def counted_new(key, message, digest):
        nonlocal calls
        calls += 1
        return original(key, message, digest)

    monkeypatch.setattr(webhook_module.hmac, "new", counted_new)
    StripeWebhookVerifier(secrets).verify(
        body=BODY,
        headers={b"stripe-signature": header},
        now=NOW,
    )
    assert calls == len(secrets)


def test_standard_webhooks_refuses_an_empty_secret_collection() -> None:
    with pytest.raises(ValueError, match="at least one non-empty"):
        StandardWebhookVerifier(())


@pytest.mark.parametrize("verifier", [StandardWebhookVerifier, StripeWebhookVerifier])
def test_multi_secret_provider_verifiers_bound_rotation_work(verifier) -> None:
    secrets = tuple(f"secret-{index}".encode() for index in range(33))

    with pytest.raises(ValueError, match="at most 32 webhook secrets"):
        verifier(secrets)


@pytest.mark.parametrize(
    ("verifier", "headers"),
    (
        (
            StandardWebhookVerifier(b"standard-secret"),
            {
                b"webhook-id": b"evt_1",
                b"webhook-timestamp": b"999999999999999999999999",
                b"webhook-signature": b"v1,AAAA",
            },
        ),
        (
            StripeWebhookVerifier(b"stripe-secret"),
            {b"stripe-signature": (b"t=999999999999999999999999,v1=not-a-signature")},
        ),
    ),
)
def test_provider_profiles_normalize_out_of_range_timestamps(verifier, headers) -> None:
    with pytest.raises(ValueError, match="invalid webhook Unix timestamp"):
        verifier.verify(body=BODY, headers=headers, now=NOW)


def test_stripe_profile_refuses_a_signature_without_a_timestamp() -> None:
    verifier = StripeWebhookVerifier(b"stripe-secret")

    with pytest.raises(ValueError, match="needs one t and at least one v1"):
        verifier.verify(
            body=BODY,
            headers={b"stripe-signature": b"v1=not-a-signature"},
            now=NOW,
        )


def test_github_profile_uses_delivery_and_event_headers() -> None:
    secret = b"github-secret"
    signature = b"sha256=" + hmac.new(secret, BODY, hashlib.sha256).hexdigest().encode("ascii")
    result = GitHubWebhookVerifier(secret).verify(
        body=BODY,
        headers={
            b"X-Hub-Signature-256": signature,
            b"X-GitHub-Delivery": b"delivery-7",
            b"X-GitHub-Event": b"push",
        },
        now=NOW,
    )
    assert (result.id, result.type) == ("delivery-7", "push")


def test_github_replay_identity_cannot_be_changed_with_an_unsigned_delivery_header() -> None:
    secret = b"github-secret"
    signature = b"sha256=" + hmac.new(secret, BODY, hashlib.sha256).hexdigest().encode("ascii")
    common = {
        b"x-hub-signature-256": signature,
        b"x-github-event": b"push",
    }
    verifier = GitHubWebhookVerifier(secret)

    first = verifier.verify(
        body=BODY,
        headers={**common, b"x-github-delivery": b"delivery-1"},
        now=NOW,
    )
    replay = verifier.verify(
        body=BODY,
        headers={**common, b"x-github-delivery": b"attacker-changed-it"},
        now=NOW,
    )

    assert first.id != replay.id
    assert first.deduplication_id == replay.deduplication_id


@pytest.mark.parametrize(
    ("verifier", "headers"),
    (
        (
            StandardWebhookVerifier(b"standard-secret"),
            {
                b"webhook-id": b"evt_1",
                b"webhook-timestamp": SECONDS,
                b"webhook-signature": b"v1,invalid",
                b"Webhook-Signature": b"v1,also-invalid",
            },
        ),
        (
            StripeWebhookVerifier(b"stripe-secret"),
            {
                b"stripe-signature": b"t=" + SECONDS + b",v1=invalid",
                b"Stripe-Signature": b"t=" + SECONDS + b",v1=also-invalid",
            },
        ),
        (
            GitHubWebhookVerifier(b"github-secret"),
            {
                b"x-hub-signature-256": b"sha256=invalid",
                b"X-Hub-Signature-256": b"sha256=also-invalid",
            },
        ),
    ),
)
def test_public_verifiers_refuse_case_variant_duplicate_headers(verifier, headers) -> None:
    with pytest.raises(ValueError, match="duplicate webhook header"):
        verifier.verify(body=BODY, headers=headers, now=NOW)
