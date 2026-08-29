from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wreath._webauthn import pack_credential, unpack_credential
from wreath.crud import SENSITIVE_FIELD, sensitive_fields
from wreath.users import SecondFactor, default_second_factor_model


@pytest.fixture(scope="module")
def model():
    return default_second_factor_model(table="redaction_probe_factors")


def test_the_credential_material_column_is_hidden_by_name(model) -> None:
    assert "secret_material" in model.__wreath_column_map__
    assert "secret_material" in sensitive_fields(model)


def test_the_harmless_columns_stay_visible(model) -> None:
    visible = set(model.__wreath_column_map__) - sensitive_fields(model)
    assert {"kind", "label", "counter", "created_at", "last_used_at"} <= visible


def test_no_second_factor_column_leaks_a_secret(model) -> None:
    hidden = sensitive_fields(model)
    for name in model.__wreath_column_map__:
        holds_secret = name == "secret_material"
        assert (name in hidden) is holds_secret


def test_a_webauthn_credential_lands_in_the_hidden_column() -> None:
    packed = pack_credential(b"credential-id", b"cose-public-key", user_verified=True)
    assert unpack_credential(packed).credential_id == b"credential-id"
    stored = SecondFactor(
        id="cred-1",
        user_id="user-1",
        kind="webauthn",
        label="Security key",
        created_at=datetime.now(UTC),
        last_used_at=None,
        material=packed,
    )
    # The dataclass keeps it out of its own repr, so a traceback cannot spill it.
    assert "cose-public-key" not in repr(stored)
    assert "credential-id" not in repr(stored)


@pytest.mark.parametrize(
    "name",
    [
        "totp_secret",
        "totp_code",
        "mfa_code",
        "otp",
        "recovery_code",
        "backup_code",
        "security_code",
        "secret_material",
        "second_factor_secret",
        "webauthn_secret",
        "passkey_secret",
    ],
)
def test_the_names_a_second_factor_invites_are_all_matched(name: str) -> None:
    assert SENSITIVE_FIELD.search(name) is not None
