from __future__ import annotations

import pytest

from wreath.crud import SENSITIVE_FIELD

#: Names a secret plausibly has. Every one must be withheld by default.
SECRETS = [
    "password",
    "passwd",
    "passphrase",
    "passcode",
    "pass_code",
    "password_hash",
    "hash",
    "hashed_password",
    "salt",
    "secret",
    "client_secret",
    "token",
    "auth_token",
    "refresh_token",
    "session_token",
    "csrf_token",
    "bearer",
    "bearer_token",
    "api_key",
    "api-key",
    "apikey",
    "private_key",
    "access_key",
    "signing_key",
    "signature_key",
    "encryption_key",
    "master_key",
    "session_key",
    "credential",
    "credentials",
    "ssn",
    "otp",
    "totp",
    "mfa",
    "cvv",
    "cvc",
    "security_code",
    "security_answer",
    "pin",
    "pin_code",
    "pin_number",
    "user_pin",
    "pw",
    "pwd",
    "user_pw",
    "account_number",
    "routing_number",
    "recovery_code",
    "backup_code",
    "recovery_answer",
]

#: Ordinary columns. Withholding any of these is a false positive that costs a
#: user real data and teaches them the deny-list cannot be trusted.
ORDINARY = [
    "id",
    "email",
    "name",
    "title",
    "description",
    "created_at",
    "updated_at",
    "owner_id",
    "shipping_address",
    "spinner",
    "pinned",
    "pinboard",
    "pin_board_id",
    "spin_count",
    "basin",
    "business_id",
    "using_legacy",
    "fabric",
    "power",
    "keyword",
    "monkey",
    "key",
    "accounts",
    "number",
    "code",
    "answer",
    "passing_grade",
    "bypass",
    "compass",
    "access_level",
    "signature_version",
    # A public key is public by definition; withholding it is simply wrong.
    "public_key",
]

#: Names this pattern does **not** catch, and is not trying to. They are
#: personal data rather than credentials, and the ambiguous three-letter
#: identifiers occur inside ordinary words too often to anchor usefully.
#: `crud_router(fields=...)` is the answer for a model holding any of them.
KNOWN_UNCOVERED = [
    "iban",
    "bic",
    "sin",
    "nin",
    "tax_id",
    "passport_number",
    "license_number",
    "dob",
    "date_of_birth",
    "nonce",
]


@pytest.mark.parametrize("name", SECRETS)
def test_a_secret_looking_column_is_withheld(name: str) -> None:
    assert SENSITIVE_FIELD.search(name), f"{name!r} would be published by default"


@pytest.mark.parametrize("name", ORDINARY)
def test_an_ordinary_column_is_not_withheld(name: str) -> None:
    assert not SENSITIVE_FIELD.search(name), f"{name!r} would be withheld by mistake"


@pytest.mark.parametrize("name", KNOWN_UNCOVERED)
def test_the_documented_gaps_are_still_gaps(name: str) -> None:
    assert not SENSITIVE_FIELD.search(name), (
        f"{name!r} is now covered; update the guide and this list deliberately"
    )


def test_short_tokens_are_anchored_to_a_name_boundary() -> None:
    assert SENSITIVE_FIELD.search("pin") and SENSITIVE_FIELD.search("pin_code")
    assert not SENSITIVE_FIELD.search("pin_board_id")
    assert SENSITIVE_FIELD.search("pw") and SENSITIVE_FIELD.search("pwd")
    assert not SENSITIVE_FIELD.search("power")
