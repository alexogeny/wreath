"""The second-factor columns are hidden by name, not by anyone remembering to.

`wreath.crud` and the GraphQL schema builder both decide what to render from a
column's *name*, via `crud.SENSITIVE_FIELD`. The plan asks for this asserted
rather than assumed, and it was worth asserting: the credential's material was
first called `material`, which that regex does not match at all, so a generated
API would have served TOTP shared secrets. The column is called
`secret_material` for exactly this reason and this file is what keeps it that
way.
"""

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
    """The one column that must never be rendered."""
    assert "secret_material" in model.__wreath_column_map__
    assert "secret_material" in sensitive_fields(model)


def test_the_harmless_columns_stay_visible(model) -> None:
    """A blanket regex that hid everything would prove nothing above."""
    visible = set(model.__wreath_column_map__) - sensitive_fields(model)
    assert {"kind", "label", "counter", "created_at", "last_used_at"} <= visible


def test_no_second_factor_column_leaks_a_secret(model) -> None:
    """Every column either is not secret, or is hidden. No third case."""
    hidden = sensitive_fields(model)
    for name in model.__wreath_column_map__:
        holds_secret = name == "secret_material"
        assert (name in hidden) is holds_secret


def test_a_webauthn_credential_lands_in_the_hidden_column() -> None:
    """Stage three stores a key and a credential id in the same `material`.

    That is why the stored shape did not move: a webauthn credential's public
    key and credential id ride in `SecondFactor.material`, which is the field the
    reference model maps to `secret_material` -- so the column the regex already
    hides is the column that carries them, and a generated CRUD or GraphQL API
    cannot serve either.
    """
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
    """Names an application is likely to reach for when it models this itself."""
    assert SENSITIVE_FIELD.search(name) is not None
