from __future__ import annotations

import pytest

import wreath._secondfactor as module
from wreath._secondfactor import totp_code, verify_totp

SECRET = b"0123456789abcdef0123"


@pytest.mark.parametrize(
    "candidate", ["12345", "12x456"], ids=("wrong-length", "non-digit")
)
def test_invalid_totp_shape_never_reaches_code_generation(
    candidate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def unexpected(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return ""

    monkeypatch.setattr(module, "totp_code", unexpected)
    assert verify_totp(SECRET, candidate, at=0) is None
    assert calls == 0


def test_totp_window_does_not_generate_a_negative_counter() -> None:
    code = totp_code(SECRET, 0)
    assert verify_totp(SECRET, code, at=0, skew=1, last_counter=-2) == 0
