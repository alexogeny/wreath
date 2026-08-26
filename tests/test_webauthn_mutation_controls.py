from __future__ import annotations

import json

import pytest

from wreath._webauthn import (
    ES256,
    WebAuthnError,
    _authority,
    _cbor_head,
    check_client_data,
    parse_cose_key,
)


def test_empty_cbor_head_is_a_structured_truncation_refusal() -> None:
    with pytest.raises(WebAuthnError, match="CBOR value is truncated"):
        _cbor_head(b"", 0)


def test_p256_refusal_names_a_short_x_coordinate() -> None:
    key = {1: 2, 3: ES256, -1: 1, -2: b"x" * 31, -3: b"y" * 32}
    with pytest.raises(WebAuthnError, match="P-256 coordinate is 32 bytes"):
        parse_cose_key(key)


@pytest.mark.parametrize(
    "origin",
    ["https", "https://", "://example.test"],
    ids=("no-separator", "no-authority", "no-scheme"),
)
def test_authority_requires_each_origin_component(origin: str) -> None:
    assert _authority(origin) is None


@pytest.mark.parametrize(
    "origin",
    ["https://[::1", "https://[::1]path", "https://[::1]/path"],
    ids=("unclosed", "tail-without-colon", "path-tail"),
)
def test_authority_refuses_each_malformed_ipv6_tail(origin: str) -> None:
    assert _authority(origin) is None


def test_authority_accepts_an_origin_without_an_explicit_port() -> None:
    assert _authority("https://example.test") == ("https", "example.test", "")
    assert _authority("https://[::1]") == ("https", "::1", "")


def test_client_data_non_object_is_a_structured_refusal() -> None:
    encoded = json.dumps([]).encode()
    with pytest.raises(WebAuthnError, match="client data is not a JSON object"):
        check_client_data(
            encoded,
            expected_type="webauthn.get",
            challenge=b"challenge",
            origins=("https://example.test",),
        )
