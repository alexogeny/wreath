from __future__ import annotations

import hmac

import pytest

from wreath._native import _core

pytestmark = pytest.mark.skipif(_core is None, reason="native _core is not built")

ALGORITHMS = [("sha256", 32), ("sha384", 48), ("sha512", 64)]
SIGNING_INPUT = b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9"


def _verify():
    assert _core is not None
    return _core.jose_verify_hs


@pytest.mark.parametrize(("digestmod", "size"), ALGORITHMS)
def test_a_correct_signature_verifies(digestmod: str, size: int) -> None:
    key = b"k" * 32
    signature = hmac.digest(key, SIGNING_INPUT, digestmod)

    assert len(signature) == size
    assert _verify()(digestmod, key, SIGNING_INPUT, signature) is True


@pytest.mark.parametrize(("digestmod", "_size"), ALGORITHMS)
def test_a_signature_from_another_key_is_refused(digestmod: str, _size: int) -> None:
    signature = hmac.digest(b"other" * 8, SIGNING_INPUT, digestmod)

    assert _verify()(digestmod, b"k" * 32, SIGNING_INPUT, signature) is False


@pytest.mark.parametrize(("digestmod", "_size"), ALGORITHMS)
def test_a_signature_over_other_input_is_refused(digestmod: str, _size: int) -> None:
    key = b"k" * 32
    signature = hmac.digest(key, SIGNING_INPUT + b"x", digestmod)

    assert _verify()(digestmod, key, SIGNING_INPUT, signature) is False


@pytest.mark.parametrize(
    "key",
    [b"", b"k", b"k" * 63, b"k" * 64, b"k" * 65, b"k" * 300, bytes(range(256))],
)
def test_hs256_agrees_with_hmac_for_any_key_length(key: bytes) -> None:
    signature = hmac.digest(key, SIGNING_INPUT, "sha256")

    assert _verify()("sha256", key, SIGNING_INPUT, signature) is True


def test_switching_keys_does_not_reuse_the_previous_schedule() -> None:
    keys = [b"a" * 32, b"b" * 32, b"a" * 32, b"c" * 200, b"b" * 32, b"a" * 32]

    for key in keys:
        good = hmac.digest(key, SIGNING_INPUT, "sha256")
        other = hmac.digest(b"z" * 32, SIGNING_INPUT, "sha256")
        assert _verify()("sha256", key, SIGNING_INPUT, good) is True, key
        assert _verify()("sha256", key, SIGNING_INPUT, other) is False, key


def test_an_hs384_signature_does_not_verify_as_hs256() -> None:
    key = b"k" * 32
    signature = hmac.digest(key, SIGNING_INPUT, "sha384")

    assert _verify()("sha256", key, SIGNING_INPUT, signature) is False


def test_a_truncated_signature_is_refused() -> None:
    key = b"k" * 32
    signature = hmac.digest(key, SIGNING_INPUT, "sha256")

    assert _verify()("sha256", key, SIGNING_INPUT, signature[:31]) is False
    assert _verify()("sha256", key, SIGNING_INPUT, signature + b"\0") is False
