from __future__ import annotations

import json

import pytest

from wreath._webauthn import (
    EDDSA,
    ES256,
    RS256,
    CoseKey,
    WebAuthnError,
    _authority,
    _der_integer,
    b64url_decode,
    b64url_encode,
    cbor_decode,
    check_client_data,
    der_signature_to_raw,
    is_loopback_host,
    origin_accepted,
    pack_credential,
    parse_attestation_object,
    parse_authenticator_data,
    parse_cose_key,
    unpack_credential,
)


def test_a_non_string_is_refused_before_anything_tries_to_decode_it() -> None:
    for bad in (None, 3, b"aGk", ["aGk"]):
        with pytest.raises(WebAuthnError, match="expected a base64url string"):
            b64url_decode(bad)


@pytest.mark.parametrize("text", ["", "a b", "a+b", "a/b", "a=b", "aGk\n", "é"])
def test_anything_outside_the_base64url_alphabet_is_refused(text: str) -> None:
    with pytest.raises(WebAuthnError, match="not base64url"):
        b64url_decode(text)


def test_missing_padding_is_tolerated_because_webauthn_omits_it() -> None:
    assert b64url_decode("aGk") == b"hi"
    assert b64url_decode("aGk=") == b"hi"


def test_an_empty_buffer_is_truncated_rather_than_an_index_error() -> None:
    with pytest.raises(WebAuthnError, match="truncated"):
        cbor_decode(b"")


def test_a_multi_byte_length_with_too_few_bytes_behind_it_is_truncated() -> None:
    with pytest.raises(WebAuthnError, match="CBOR length is truncated"):
        cbor_decode(b"\x19\x01")


def test_trailing_bytes_after_a_complete_item_are_refused() -> None:
    with pytest.raises(WebAuthnError, match="trailing bytes"):
        cbor_decode(b"\x01\x02")


@pytest.mark.parametrize("initial", [0x9F, 0xBF, 0x5F, 0x7F])
def test_indefinite_length_encodings_are_refused(initial: int) -> None:
    with pytest.raises(WebAuthnError, match="indefinite-length"):
        cbor_decode(bytes([initial]) + b"\x01\xff")


@pytest.mark.parametrize("minor", [28, 29, 30])
def test_reserved_additional_information_is_refused_and_named(minor: int) -> None:
    with pytest.raises(WebAuthnError, match=f"reserved CBOR additional information: {minor}"):
        cbor_decode(bytes([minor]))


def test_a_byte_string_longer_than_the_buffer_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="runs past the end"):
        cbor_decode(b"\x45ab")


def test_an_enormous_declared_length_is_refused_rather_than_allocated() -> None:
    with pytest.raises(WebAuthnError, match="runs past the end"):
        cbor_decode(b"\x5b" + b"\xff" * 8)


def test_an_array_claiming_more_elements_than_there_are_bytes_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="array is longer than the buffer"):
        cbor_decode(b"\x85ab")


def test_a_map_claiming_more_pairs_than_there_are_bytes_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="map is longer than the buffer"):
        cbor_decode(b"\xa5ab")


def test_nesting_deeper_than_the_bound_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="nesting is too deep"):
        cbor_decode(b"\x81" * 12 + b"\x01")


@pytest.mark.parametrize("initial", [0xF9, 0xFA, 0xFB])
def test_floats_are_refused(initial: int) -> None:
    with pytest.raises(WebAuthnError, match="floats are not accepted"):
        cbor_decode(bytes([initial]) + b"\x00" * 8)


def test_undefined_is_refused_and_the_message_names_the_value() -> None:
    with pytest.raises(WebAuthnError, match="unsupported CBOR simple value: 23"):
        cbor_decode(b"\xf7")


def test_tags_are_refused() -> None:
    with pytest.raises(WebAuthnError, match="tags are not accepted"):
        cbor_decode(b"\xc1\x01")


def test_text_that_is_not_valid_utf8_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="not valid UTF-8"):
        cbor_decode(b"\x61\xff")


@pytest.mark.parametrize(
    "encoded,description",
    [
        (b"\xa1\x41a\x01", "a byte-string key"),
        (b"\xa1\xf5\x01", "a boolean key"),
        (b"\xa1\x81\x01\x01", "an array key"),
    ],
)
def test_a_map_key_that_is_not_an_integer_or_text_is_refused(
    encoded: bytes, description: str
) -> None:
    with pytest.raises(WebAuthnError, match="map keys must be integers or text"):
        cbor_decode(encoded)


def test_a_duplicate_map_key_is_refused_rather_than_last_one_winning() -> None:
    with pytest.raises(WebAuthnError, match="duplicate CBOR map key"):
        cbor_decode(b"\xa2\x01\x01\x01\x02")


def test_the_shapes_an_attestation_object_actually_uses_still_decode() -> None:
    assert cbor_decode(b"\x01") == 1
    assert cbor_decode(b"\x20") == -1
    assert cbor_decode(b"\x19\x01\x00") == 256
    assert cbor_decode(b"\x42ab") == b"ab"
    assert cbor_decode(b"\x62hi") == "hi"
    assert cbor_decode(b"\x82\x01\x02") == [1, 2]
    assert cbor_decode(b"\xa1\x01\x02") == {1: 2}
    assert cbor_decode(b"\xf4") is False
    assert cbor_decode(b"\xf5") is True
    assert cbor_decode(b"\xf6") is None


def _cose(**entries) -> dict:
    """A COSE map from readable names, so each test says what it changed."""
    label = {"kty": 1, "alg": 3, "crv": -1, "x": -2, "y": -3}
    return {label[name]: value for name, value in entries.items()}


def test_a_cose_key_that_is_not_a_map_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="must be a CBOR map"):
        parse_cose_key(b"\x01")


def test_rs256_is_refused_by_name_rather_than_as_merely_unsupported() -> None:
    with pytest.raises(WebAuthnError, match="RS256"):
        parse_cose_key(_cose(alg=RS256, kty=2, crv=1, x=b"\x01" * 32, y=b"\x02" * 32))


@pytest.mark.parametrize("algorithm", [None, 0, -35, -257 + 1, "ES256"])
def test_any_other_algorithm_is_refused_and_repeated_back(algorithm) -> None:
    with pytest.raises(WebAuthnError, match="unsupported COSE algorithm"):
        parse_cose_key(_cose(alg=algorithm, kty=2, crv=1, x=b"\x01" * 32))


def test_a_key_with_no_x_coordinate_is_refused() -> None:
    for missing in (None, "not bytes", 1):
        with pytest.raises(WebAuthnError, match="no public coordinate"):
            parse_cose_key(_cose(alg=ES256, kty=2, crv=1, x=missing))


def test_eddsa_must_be_okp_over_ed25519_and_nothing_else() -> None:
    with pytest.raises(WebAuthnError, match="OKP over Ed25519"):
        parse_cose_key(_cose(alg=EDDSA, kty=2, crv=6, x=b"\x01" * 32))
    with pytest.raises(WebAuthnError, match="OKP over Ed25519"):
        parse_cose_key(_cose(alg=EDDSA, kty=1, crv=1, x=b"\x01" * 32))


@pytest.mark.parametrize("length", [0, 31, 33, 64])
def test_an_ed25519_public_key_must_be_exactly_thirty_two_bytes(length: int) -> None:
    with pytest.raises(WebAuthnError, match="32 bytes"):
        parse_cose_key(_cose(alg=EDDSA, kty=1, crv=6, x=b"\x01" * length))


def test_a_well_formed_ed25519_key_parses() -> None:
    key = parse_cose_key(_cose(alg=EDDSA, kty=1, crv=6, x=b"\x01" * 32))
    assert key == CoseKey(algorithm=EDDSA, x=b"\x01" * 32)


def test_es256_must_be_ec2_over_p256_and_nothing_else() -> None:
    with pytest.raises(WebAuthnError, match="EC2 over P-256"):
        parse_cose_key(_cose(alg=ES256, kty=1, crv=1, x=b"\x01" * 32, y=b"\x02" * 32))
    with pytest.raises(WebAuthnError, match="EC2 over P-256"):
        parse_cose_key(_cose(alg=ES256, kty=2, crv=6, x=b"\x01" * 32, y=b"\x02" * 32))


def test_an_es256_key_with_no_y_coordinate_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="no y coordinate"):
        parse_cose_key(_cose(alg=ES256, kty=2, crv=1, x=b"\x01" * 32))


@pytest.mark.parametrize("lengths", [(31, 32), (32, 31), (33, 32), (32, 33)])
def test_a_p256_coordinate_must_be_exactly_thirty_two_bytes(lengths) -> None:
    x_len, y_len = lengths
    with pytest.raises(WebAuthnError, match="32 bytes"):
        parse_cose_key(_cose(alg=ES256, kty=2, crv=1, x=b"\x01" * x_len, y=b"\x02" * y_len))


# `der_signature_to_raw`'s docstring states the stake: "A parser that accepts
# `02 02 00 7f` alongside `02 01 7f` accepts two encodings of one signature, which is a
# malleability nobody needs to hand out for free." Every refusal enforcing that was
# unkilled, so the malleability protection was unverified -- and the minimal-encoding
# rule is the one an attacker probes, because it is the one implementations skip.


def _der(r: bytes, s: bytes) -> bytes:
    """A DER SEQUENCE of two INTEGERs with the given content octets, verbatim.

    Content is passed already-encoded rather than as integers, because most of these
    tests are *about* encodings an integer round trip would silently normalise away.
    """
    body = bytes([0x02, len(r)]) + r + bytes([0x02, len(s)]) + s
    return bytes([0x30, len(body)]) + body


def test_a_well_formed_es256_signature_becomes_sixty_four_raw_bytes() -> None:
    raw = der_signature_to_raw(_der(b"\x7f" + b"\x11" * 31, b"\x2a" + b"\x22" * 31))
    assert len(raw) == 64
    assert raw[:32] == b"\x7f" + b"\x11" * 31
    assert raw[32:] == b"\x2a" + b"\x22" * 31


def test_a_short_component_is_left_padded_to_thirty_two_bytes() -> None:
    raw = der_signature_to_raw(_der(b"\x01", b"\x02"))
    assert raw == b"\x00" * 31 + b"\x01" + b"\x00" * 31 + b"\x02"


@pytest.mark.parametrize(
    "signature,why",
    [
        (b"", "empty"),
        (b"\x30\x06\x02\x01\x01\x02", "under the eight-byte floor"),
        (b"\x31\x08\x02\x01\x01\x02\x01\x01", "not a SEQUENCE tag"),
        (b"\x02\x08\x02\x01\x01\x02\x01\x01", "an INTEGER where the SEQUENCE goes"),
    ],
)
def test_something_that_is_not_a_der_sequence_is_refused(signature: bytes, why: str) -> None:
    with pytest.raises(WebAuthnError, match="must be a DER SEQUENCE"):
        der_signature_to_raw(signature)


def test_der_long_form_length_is_refused_even_though_it_is_legal_der() -> None:
    with pytest.raises(WebAuthnError, match="long-form length is not accepted"):
        der_signature_to_raw(b"\x30\x81\x08\x02\x01\x01\x02\x01\x01")


@pytest.mark.parametrize("declared", [0x07, 0x09])
def test_a_sequence_length_that_disagrees_with_the_buffer_is_refused(declared: int) -> None:
    good = _der(b"\x01", b"\x02")
    with pytest.raises(WebAuthnError, match="length does not match"):
        der_signature_to_raw(bytes([good[0], declared]) + good[2:])


def test_trailing_bytes_after_the_two_integers_are_refused() -> None:
    body = b"\x02\x01\x01\x02\x01\x02\x02\x01\x03"
    with pytest.raises(WebAuthnError, match="trailing bytes after the DER signature"):
        der_signature_to_raw(bytes([0x30, len(body)]) + body)


@pytest.mark.parametrize(
    "signature,why",
    [
        # The declared SEQUENCE length must be correct, or the length check fires
        # first and this tests the wrong refusal.
        (b"\x30\x06\x03\x01\x01\x02\x01\x01", "first element is not an INTEGER"),
        (b"\x30\x06\x02\x01\x01\x03\x01\x01", "second element is not an INTEGER"),
    ],
)
def test_an_element_that_is_not_a_der_integer_is_refused(signature: bytes, why: str) -> None:
    with pytest.raises(WebAuthnError, match="expected a DER INTEGER"):
        der_signature_to_raw(signature)


@pytest.mark.parametrize(
    "r_length_byte,why",
    [
        (0x00, "zero-length INTEGER"),
        (0x80, "long-form INTEGER length"),
        (0x40, "INTEGER length past the end of the buffer"),
    ],
)
def test_a_malformed_der_integer_length_is_refused(r_length_byte: int, why: str) -> None:
    body = bytes([0x02, r_length_byte]) + b"\x01" + b"\x02\x01\x02"
    with pytest.raises(WebAuthnError, match="malformed DER INTEGER length"):
        der_signature_to_raw(bytes([0x30, len(body)]) + body)


def test_a_missing_second_der_integer_is_a_structured_refusal() -> None:
    with pytest.raises(WebAuthnError, match="expected a DER INTEGER"):
        der_signature_to_raw(b"\x30\x06\x02\x04\x01\x01\x01\x01")


def test_a_long_form_integer_length_is_refused_before_reading_its_body() -> None:
    with pytest.raises(WebAuthnError, match="malformed DER INTEGER length"):
        _der_integer(b"\x02\x81" + b"\x01" * 129, 0)


def test_a_negative_signature_component_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="must not be negative"):
        der_signature_to_raw(_der(b"\x80\x01", b"\x02"))


def test_a_non_minimal_der_integer_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="non-minimal DER INTEGER"):
        der_signature_to_raw(_der(b"\x00\x7f", b"\x02"))
    # The legitimate leading zero, which the same rule must not reject: the next octet
    # has its high bit set, so without the zero the INTEGER would read as negative.
    # It survives into the raw form because the magnitude is 31 bytes and pads back to
    # 32 -- the padding zero and the DER zero are the same byte here.
    content = b"\x00\x80" + b"\x00" * 30
    raw = der_signature_to_raw(_der(content, b"\x02"))
    assert raw[:32] == content
    assert int.from_bytes(raw[:32], "big") == 0x80 << (30 * 8)


def test_a_zero_signature_component_is_out_of_range() -> None:
    with pytest.raises(WebAuthnError, match="out of range"):
        der_signature_to_raw(_der(b"\x00", b"\x02"))
    with pytest.raises(WebAuthnError, match="out of range"):
        der_signature_to_raw(_der(b"\x01", b"\x00"))


def _auth_data(flags: int, tail: bytes = b"") -> bytes:
    """A 37-byte authenticator data header plus whatever follows it."""
    return b"\xaa" * 32 + bytes([flags]) + b"\x00\x00\x00\x01" + tail


def test_authenticator_data_shorter_than_its_header_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="too short"):
        parse_authenticator_data(b"\xaa" * 36)


def test_attested_credential_data_that_is_truncated_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="attested credential data is truncated"):
        parse_authenticator_data(_auth_data(0x40, b"\x00" * 17))


def test_a_credential_id_longer_than_the_spec_allows_is_refused() -> None:
    tail = b"\x00" * 16 + (1024).to_bytes(2, "big")
    with pytest.raises(WebAuthnError, match="1023 bytes"):
        parse_authenticator_data(_auth_data(0x40, tail))


def test_a_credential_id_running_past_the_buffer_is_refused() -> None:
    tail = b"\x00" * 16 + (64).to_bytes(2, "big") + b"\x01" * 8
    with pytest.raises(WebAuthnError, match="runs past the end of the buffer"):
        parse_authenticator_data(_auth_data(0x40, tail))


def test_declared_extensions_are_consumed_as_one_complete_cbor_value() -> None:
    parsed = parse_authenticator_data(_auth_data(0x80, b"\xa0"))
    assert parsed.flags == 0x80


def _none_attestation(auth_data: bytes) -> bytes:
    assert len(auth_data) < 256
    return b"\xa2\x63fmt\x64none\x68authData\x58" + bytes([len(auth_data)]) + auth_data


def test_none_attestation_requires_attested_credential_data() -> None:
    with pytest.raises(WebAuthnError, match="no attested credential data"):
        parse_attestation_object(_none_attestation(_auth_data(0)))


def test_none_attestation_refuses_an_empty_credential_identifier() -> None:
    cose = b"\xa4\x01\x01\x03\x27\x20\x06\x21\x58\x20" + b"x" * 32
    auth_data = _auth_data(0x40, b"\x00" * 16 + b"\x00\x00" + cose)
    with pytest.raises(WebAuthnError, match="no attested credential data"):
        parse_attestation_object(_none_attestation(auth_data))


def test_an_attestation_object_that_is_not_a_map_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="must be a CBOR map"):
        parse_attestation_object(b"\x81\x01")


def test_an_attestation_object_without_authenticator_data_is_refused() -> None:
    for encoded in (b"\xa1\x63fmt\x64none", b"\xa2\x63fmt\x64none\x68authData\x01"):
        with pytest.raises(WebAuthnError, match="carries no authenticator data"):
            parse_attestation_object(encoded)


def test_an_attestation_format_other_than_none_is_refused_and_named() -> None:
    encoded = b"\xa2\x63fmt\x66packed\x68authData\x41\x00"
    with pytest.raises(WebAuthnError, match="packed"):
        parse_attestation_object(encoded)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0",
        "127.0.0.1.2",
        "126.0.0.1",
        "127.a.0.1",
        "127.0000.0.1",
        "127.256.0.1",
    ],
)
def test_only_well_formed_127_slash_8_addresses_are_loopback(host: str) -> None:
    assert is_loopback_host(host) is False


@pytest.mark.parametrize(
    "origin",
    [
        "localhost",
        "://localhost",
        "https://",
        "https://:8000",
        "https://é:8000",
        "https://localhost:é",
        "https://local@host:8000",
        "https://localhost:80/path",
        "https://localhost:80:81",
    ],
)
def test_malformed_authorities_are_not_normalized(origin: str) -> None:
    assert _authority(origin) is None


def test_authority_accepts_bare_hosts_and_bracketed_ipv6() -> None:
    assert _authority("https://example.test") == ("https", "example.test", "")
    assert _authority("http://[::1]:8000") == ("http", "::1", "8000")
    assert origin_accepted("http://[::1]:8000", ("http://[::1]",)) is True


@pytest.mark.parametrize("presented", [None, 7, ["challenge"]])
def test_client_data_challenge_must_be_a_string(presented) -> None:
    body = json.dumps(
        {"type": "webauthn.create", "challenge": presented, "origin": "https://h"}
    ).encode()
    with pytest.raises(WebAuthnError, match="carries no challenge"):
        check_client_data(
            body,
            expected_type="webauthn.create",
            challenge=b"challenge",
            origins=("https://h",),
        )


@pytest.mark.parametrize("origin", [None, 7, ["https://h"]])
def test_client_data_origin_must_be_a_string(origin) -> None:
    body = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": b64url_encode(b"challenge"),
            "origin": origin,
        }
    ).encode()
    with pytest.raises(WebAuthnError, match="origin"):
        check_client_data(
            body,
            expected_type="webauthn.create",
            challenge=b"challenge",
            origins=("https://h",),
        )


def test_stored_credentials_refuse_a_short_header_and_unknown_version() -> None:
    with pytest.raises(WebAuthnError, match="truncated"):
        unpack_credential(b"wa1")
    material = bytearray(pack_credential(b"id", b"key", user_verified=False))
    material[3] = 2
    with pytest.raises(WebAuthnError, match="unknown.*version: 2"):
        unpack_credential(bytes(material))
