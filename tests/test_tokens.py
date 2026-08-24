"""First-party purpose-scoped action tokens."""

from __future__ import annotations

import pytest

from wreath.tokens import (
    ActionTokens,
    MemoryTokenLedger,
    TokenPurpose,
    token_fingerprint,
)

KEY_A = b"a" * 32
KEY_B = b"b" * 32


def test_token_is_purpose_and_context_bound_and_rotates_by_key_id() -> None:
    purposes = [TokenPurpose("invite", 60), TokenPurpose("verify", 30)]
    issuer = ActionTokens({"old": KEY_A}, current="old", purposes=purposes)
    token = issuer.issue("invite", "user-7", bound="org-2", now=100)
    verifier = ActionTokens(
        {"new": KEY_B, "old": KEY_A}, current="new", purposes=purposes
    )
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
