from __future__ import annotations

import pytest

from wreath.auth import jwk_thumbprint, jwk_thumbprint_uri

RFC_7638_RSA_JWK = {
    "kty": "RSA",
    "n": (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAt"
        "VT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn6"
        "4tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FD"
        "W2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n9"
        "1CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINH"
        "aQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    ),
    "e": "AQAB",
    "alg": "RS256",
    "kid": "2011-04-29",
}


def test_rfc_7638_thumbprint_vector_ignores_non_required_members() -> None:
    assert jwk_thumbprint(RFC_7638_RSA_JWK) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_rfc_9278_thumbprint_uri_vector() -> None:
    assert jwk_thumbprint_uri(RFC_7638_RSA_JWK) == (
        "urn:ietf:params:oauth:jwk-thumbprint:sha-256:"
        "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"
    )


@pytest.mark.parametrize(
    "jwk",
    [
        {"kty": "RSA", "n": "modulus"},
        {"kty": "EC", "crv": "P-256", "x": "x"},
        {"kty": "unsupported", "x": "x"},
        {"kty": "RSA", "n": 1, "e": "AQAB"},
    ],
)
def test_thumbprint_refuses_incomplete_unsupported_or_non_string_jwks(jwk) -> None:
    with pytest.raises(ValueError, match="JWK thumbprint"):
        jwk_thumbprint(jwk)


def test_thumbprint_uri_refuses_an_unimplemented_hash_identifier() -> None:
    with pytest.raises(ValueError, match="sha-256"):
        jwk_thumbprint_uri(RFC_7638_RSA_JWK, hash_name="sha-512")
