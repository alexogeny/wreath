from __future__ import annotations

import hmac
from typing import cast

import pytest

from wreath import _json
from wreath._b64 import b64url_decode, b64url_encode
from wreath.tokens import (
    MAX_TOKEN_BYTES,
    ActionTokens,
    MemoryTokenLedger,
    TokenPurpose,
    token_fingerprint,
)

KEY_A = b"a" * 32
KEY_B = b"b" * 32


def signed_payload(token: str, **changes: object) -> str:
    prefix, body, _ = token.split(".")
    payload = _json.loads(b64url_decode(body))
    assert isinstance(payload, dict)
    payload.update(changes)
    rewritten = b64url_encode(_json.dumps(payload))
    mac = b64url_encode(hmac.digest(KEY_A, rewritten.encode("ascii"), "sha256"))
    return f"{prefix}.{rewritten}.{mac}"


def signed_value(value: object) -> str:
    body = b64url_encode(_json.dumps(value))
    mac = b64url_encode(hmac.digest(KEY_A, body.encode("ascii"), "sha256"))
    return f"w1.{body}.{mac}"


def test_token_is_purpose_and_context_bound_and_rotates_by_key_id() -> None:
    purposes = [TokenPurpose("invite", 60), TokenPurpose("verify", 30)]
    issuer = ActionTokens({"old": KEY_A}, current="old", purposes=purposes)
    token = issuer.issue("invite", "user-7", bound="org-2", now=100)
    verifier = ActionTokens({"new": KEY_B, "old": KEY_A}, current="new", purposes=purposes)
    claims = verifier.verify("invite", token, bound="org-2", now=120)
    assert claims is not None
    assert claims.subject == "user-7" and claims.key_id == "old"
    assert verifier.verify("verify", token, bound="org-2", now=120) is None
    assert verifier.verify("invite", token, bound="another", now=120) is None
    assert verifier.verify("invite", token + "x", bound="org-2", now=120) is None
    assert verifier.verify("invite", token, bound="org-2", now=161) is None


def test_single_use_token_is_consumed_exactly_once() -> None:
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("reset", 60, single_use=True)],
        ledger=MemoryTokenLedger(max_entries=2),
    )
    token = tokens.issue("reset", "user-1", now=100)
    assert tokens.verify("reset", token, now=101) is not None
    assert tokens.verify("reset", token, now=101) is None


def test_single_use_declaration_requires_an_explicit_ledger() -> None:
    with pytest.raises(ValueError, match="no ledger"):
        ActionTokens(
            {"active": KEY_A},
            current="active",
            purposes=[TokenPurpose("reset", 60, single_use=True)],
        )


def test_token_purpose_names_must_be_unique() -> None:
    duplicate = TokenPurpose("invite", 60)
    with pytest.raises(ValueError, match="purpose 'invite' is declared twice"):
        ActionTokens(
            {"active": KEY_A},
            current="active",
            purposes=[duplicate, duplicate],
        )


def test_token_fingerprint_is_stable_and_does_not_include_the_token() -> None:
    fingerprint = token_fingerprint("secret-token")
    assert fingerprint == token_fingerprint("secret-token")
    assert "secret-token" not in fingerprint and len(fingerprint) == 16


@pytest.mark.parametrize(
    ("name", "ttl"),
    [("", 60), ("x" * 129, 60), ("invite", True), ("invite", "60"), ("invite", 0)],
    ids=("empty-name", "long-name", "boolean-ttl", "string-ttl", "zero-ttl"),
)
def test_token_purpose_refuses_each_invalid_declaration(name: object, ttl: object) -> None:
    with pytest.raises(ValueError, match="TokenPurpose"):
        TokenPurpose(cast("str", name), cast("int", ttl))


@pytest.mark.parametrize("max_entries", [True, "2", 0], ids=("boolean", "string", "zero"))
def test_memory_ledger_refuses_each_invalid_capacity(max_entries: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        MemoryTokenLedger(max_entries=cast("int", max_entries))


@pytest.mark.parametrize("key_id", [1, "", "k" * 65], ids=("non-string", "empty", "long"))
def test_action_tokens_refuses_each_invalid_key_id(key_id: object) -> None:
    with pytest.raises(ValueError, match="key ids"):
        ActionTokens(
            {cast("str", key_id): KEY_A},
            current="active",
            purposes=[TokenPurpose("invite", 60)],
        )


@pytest.mark.parametrize("secret", ["x" * 32, b"short"], ids=("non-bytes", "short"))
def test_action_tokens_refuses_each_invalid_secret(secret: object) -> None:
    with pytest.raises(ValueError, match="bytes of at least"):
        ActionTokens(
            {"active": cast("bytes", secret)},
            current="active",
            purposes=[TokenPurpose("invite", 60)],
        )


def test_action_tokens_refuses_missing_current_key_and_empty_or_wrong_purposes() -> None:
    purpose = TokenPurpose("invite", 60)
    with pytest.raises(ValueError, match="not present in keys"):
        ActionTokens({"active": KEY_A}, current="missing", purposes=[purpose])
    with pytest.raises(ValueError, match="at least one TokenPurpose"):
        ActionTokens({"active": KEY_A}, current="active", purposes=[])
    with pytest.raises(TypeError, match="must contain TokenPurpose"):
        ActionTokens(
            {"active": KEY_A},
            current="active",
            purposes=cast("list[TokenPurpose]", [object()]),
        )


@pytest.mark.parametrize(
    "maximum",
    [True, "128", 127, MAX_TOKEN_BYTES + 1],
    ids=("boolean", "string", "below-minimum", "above-maximum"),
)
def test_action_tokens_refuses_each_invalid_token_size_limit(maximum: object) -> None:
    with pytest.raises(ValueError, match="integer from 128"):
        ActionTokens(
            {"active": KEY_A},
            current="active",
            purposes=[TokenPurpose("invite", 60)],
            max_token_bytes=cast("int", maximum),
        )


@pytest.mark.parametrize("subject", [1, "", "s" * 1025], ids=("non-string", "empty", "long"))
def test_issue_refuses_each_invalid_subject(subject: object) -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    with pytest.raises(ValueError, match="subject"):
        tokens.issue("invite", cast("str", subject), now=100)


@pytest.mark.parametrize("bound", [None, "b" * 1025], ids=("non-string", "long"))
def test_issue_refuses_each_invalid_bound_value(bound: object) -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    with pytest.raises(ValueError, match="bound value"):
        tokens.issue("invite", "subject", bound=cast("str", bound), now=100)


def test_issue_refuses_a_token_over_the_declared_wire_limit() -> None:
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("invite", 60)],
        max_token_bytes=128,
    )
    with pytest.raises(ValueError, match="longer than max_token_bytes=128"):
        tokens.issue("invite", "subject", now=100)


def test_implicit_clock_is_used_for_issue_and_verify() -> None:
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("invite", 60)],
        clock=lambda: 100,
    )
    token = tokens.issue("invite", "subject")
    claims = tokens.verify("invite", token)
    assert claims is not None
    assert claims.issued_at == 100


def test_single_use_issue_refuses_a_full_ledger() -> None:
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("reset", 60, single_use=True)],
        ledger=MemoryTokenLedger(max_entries=1),
    )
    tokens.issue("reset", "first", now=100)
    with pytest.raises(RuntimeError, match="ledger is full"):
        tokens.issue("reset", "second", now=100)


def test_oversize_single_use_issue_releases_its_registered_nonce() -> None:
    class Ledger:
        def __init__(self) -> None:
            self.registered: list[str] = []
            self.consumed: list[str] = []

        def register(self, token_id: str, *, ttl: int, now: float) -> bool:
            self.registered.append(token_id)
            return True

        def consume(self, token_id: str, *, now: float) -> bool:
            self.consumed.append(token_id)
            return True

    ledger = Ledger()
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("reset", 60, single_use=True)],
        ledger=ledger,
        max_token_bytes=128,
    )
    with pytest.raises(ValueError, match="longer than max_token_bytes=128"):
        tokens.issue("reset", "subject", now=100)
    assert ledger.consumed == ledger.registered


def test_single_use_issue_refuses_if_its_declared_ledger_is_lost() -> None:
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("reset", 60, single_use=True)],
        ledger=MemoryTokenLedger(),
    )
    tokens._ledger = None

    with pytest.raises(RuntimeError, match="lost its declared ledger"):
        tokens.issue("reset", "subject", now=100)


def test_single_use_verify_refuses_if_its_declared_ledger_is_lost() -> None:
    tokens = ActionTokens(
        {"active": KEY_A},
        current="active",
        purposes=[TokenPurpose("reset", 60, single_use=True)],
        ledger=MemoryTokenLedger(),
    )
    token = tokens.issue("reset", "subject", now=100)
    tokens._ledger = None

    with pytest.raises(RuntimeError, match="lost its declared ledger"):
        tokens.verify("reset", token, now=101)


@pytest.mark.parametrize(
    "token",
    [None, "x" * 4097, "w2.eA.eA"],
    ids=("non-string", "over-limit", "wrong-prefix"),
)
def test_verify_refuses_invalid_outer_token_shapes(token: object) -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    assert tokens.verify("invite", cast("str", token), now=100) is None


def test_verify_refuses_a_well_formed_token_with_the_wrong_prefix() -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    token = tokens.issue("invite", "subject", now=100)
    _, body, mac = token.split(".")
    assert tokens.verify("invite", f"w2.{body}.{mac}", now=100) is None


def test_verify_refuses_a_valid_token_over_its_own_wire_limit() -> None:
    purpose = TokenPurpose("invite", 60)
    issuer = ActionTokens({"active": KEY_A}, current="active", purposes=[purpose])
    verifier = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[purpose], max_token_bytes=128
    )
    token = issuer.issue("invite", "subject", now=100)
    assert len(token.encode("utf-8")) > verifier.max_token_bytes

    assert verifier.verify("invite", token, now=100) is None


def test_verify_refuses_non_object_and_wrong_field_set_payloads() -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    valid = tokens.issue("invite", "subject", now=100)
    assert tokens.verify("invite", signed_value([]), now=100) is None
    assert tokens.verify("invite", signed_payload(valid, extra=True), now=100) is None


@pytest.mark.parametrize(
    ("changes", "now"),
    [
        ({"v": 2}, 120),
        ({"p": "other"}, 120),
        ({"k": 1}, 120),
        ({"iat": True, "exp": 61}, 1),
        ({"iat": 100.0, "exp": 160}, 120),
        ({"iat": -59, "exp": True}, 0),
        ({"exp": 160.0}, 120),
        ({"exp": 161}, 120),
        ({"s": 1}, 120),
        ({"j": 1}, 120),
    ],
    ids=(
        "version",
        "purpose",
        "key-id-type",
        "issued-boolean",
        "issued-type",
        "expires-boolean",
        "expires-type",
        "duration",
        "subject-type",
        "token-id-type",
    ),
)
def test_verify_refuses_each_invalid_signed_claim(changes: dict[str, object], now: int) -> None:
    purposes = [TokenPurpose("invite", 60), TokenPurpose("other", 60)]
    tokens = ActionTokens({"active": KEY_A}, current="active", purposes=purposes)
    token = tokens.issue("invite", "subject", now=100)
    assert tokens.verify("invite", signed_payload(token, **changes), now=now) is None


def test_verify_refuses_a_token_issued_in_the_future() -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    token = tokens.issue("invite", "subject", now=100)
    assert tokens.verify("invite", token, now=99) is None


def test_unknown_purpose_is_refused_before_issue_or_verify() -> None:
    tokens = ActionTokens(
        {"active": KEY_A}, current="active", purposes=[TokenPurpose("invite", 60)]
    )
    with pytest.raises(ValueError, match="purpose 'missing' is not declared"):
        tokens.issue("missing", "subject")
    with pytest.raises(ValueError, match="purpose 'missing' is not declared"):
        tokens.verify("missing", "token")
