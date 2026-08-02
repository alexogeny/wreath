"""The signature base, pinned against RFC 9421's own vectors.

The base is where implementations disagree, so a test that only drives this
module's signer through this module's verifier proves the base is
self-consistent -- not that it is correct. These tests pin it two ways:

* against the RFC's published Ed25519 vector (§B.1.4 key, §B.2.6 signature),
  byte for byte, which is an independent party's arithmetic;
* against `cryptography` as a real, independent signer, which is a dev-group
  dependency and never a runtime one.
"""

from __future__ import annotations

import base64

import pytest

from wreath.signatures import (
    RequestMessage,
    SignatureError,
    signature_base,
)

# RFC 9421 §B.1.4 -- the published Ed25519 test key.
RFC_PRIVATE = base64.urlsafe_b64decode("n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU=")
RFC_PUBLIC = base64.urlsafe_b64decode("JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs=")
RFC_KEY_ID = "test-key-ed25519"

# RFC 9421 §B.2.6 -- the signature over the test request below.
RFC_SIGNATURE = base64.b64decode(
    "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu"
    "4A0w6vuQv5lIp5WPpBKRCw=="
)

RFC_COMPONENTS = (
    ("date", {}),
    ("@method", {}),
    ("@path", {}),
    ("@authority", {}),
    ("content-type", {}),
    ("content-length", {}),
)
RFC_PARAMS = {"created": 1618884473, "keyid": RFC_KEY_ID}

RFC_EXPECTED_BASE = (
    b'"date": Tue, 20 Apr 2021 02:07:55 GMT\n'
    b'"@method": POST\n'
    b'"@path": /foo\n'
    b'"@authority": example.com\n'
    b'"content-type": application/json\n'
    b'"content-length": 18\n'
    b'"@signature-params": ("date" "@method" "@path" "@authority" '
    b'"content-type" "content-length");created=1618884473;keyid="test-key-ed25519"'
)


def rfc_message() -> RequestMessage:
    """RFC 9421 §B.2's test request."""
    return RequestMessage(
        method="POST",
        scheme="https",
        authority="example.com",
        path="/foo",
        query=b"param=Value&Pet=dog",
        headers={
            b"host": b"example.com",
            b"date": b"Tue, 20 Apr 2021 02:07:55 GMT",
            b"content-type": b"application/json",
            b"content-length": b"18",
        },
    )


def test_base_matches_the_rfc_byte_for_byte():
    assert signature_base(rfc_message(), RFC_COMPONENTS, RFC_PARAMS) == RFC_EXPECTED_BASE


def test_rfc_signature_verifies_against_the_rfc_key():
    """The published signature over the published base with the published key.

    Independent arithmetic: if the base were wrong by one byte this fails.
    """
    from wreath._auth._ecverify import verify_ed25519

    base = signature_base(rfc_message(), RFC_COMPONENTS, RFC_PARAMS)
    assert verify_ed25519(RFC_PUBLIC, base, RFC_SIGNATURE) is True


def test_an_independent_signer_agrees_with_this_base():
    """`cryptography` signing our base reproduces the RFC's signature exactly.

    Ed25519 is deterministic, so this is an equality check rather than a
    round trip -- it confirms the base *and* the recorded vector at once.
    """
    # Imported rather than `importorskip`ed: `cryptography` is in the `dev`
    # group and `tests/test_dev_environment.py` asserts it is installed, so a
    # skip here would hide the one failure this test exists to catch.
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.from_private_bytes(RFC_PRIVATE)
    base = signature_base(rfc_message(), RFC_COMPONENTS, RFC_PARAMS)
    assert key.sign(base) == RFC_SIGNATURE


def test_a_signature_over_the_wrong_base_does_not_verify():
    """The falsifier. A valid signature over a *different* request must fail.

    Without this, every other test here passes for an implementation that
    signs the empty string.
    """
    from wreath._auth._ecverify import verify_ed25519

    tampered = RequestMessage(
        method="GET",  # the only change
        scheme="https",
        authority="example.com",
        path="/foo",
        query=b"param=Value&Pet=dog",
        headers=rfc_message().headers,
    )
    base = signature_base(tampered, RFC_COMPONENTS, RFC_PARAMS)
    assert verify_ed25519(RFC_PUBLIC, base, RFC_SIGNATURE) is False


def test_reordering_covered_components_changes_the_base():
    """Component order is part of the signature, not a set."""
    swapped = (RFC_COMPONENTS[1], RFC_COMPONENTS[0], *RFC_COMPONENTS[2:])
    assert signature_base(rfc_message(), swapped, RFC_PARAMS) != RFC_EXPECTED_BASE


def test_derived_components():
    message = RequestMessage(
        method="get",
        scheme="HTTPS",
        authority="example.com",
        path="/a/b",
        query=b"x=1&y=two%20words",
        headers={b"host": b"example.com"},
    )
    def one(name, params=None):
        base = signature_base(message, ((name, params or {}),), {"created": 1})
        return base.split(b"\n")[0].split(b": ", 1)[1].decode()

    assert one("@method") == "GET"
    assert one("@scheme") == "https"
    assert one("@authority") == "example.com"
    assert one("@path") == "/a/b"
    assert one("@query") == "?x=1&y=two%20words"
    assert one("@request-target") == "/a/b?x=1&y=two%20words"
    assert one("@target-uri") == "https://example.com/a/b?x=1&y=two%20words"
    assert one("@query-param", {"name": "y"}) == "two%20words"


def test_empty_query_still_covers_a_question_mark():
    """RFC 9421 §2.2.7. "No query" and "empty query" must not sign alike."""
    message = RequestMessage(
        method="GET", scheme="https", authority="h", path="/", query=b""
    )
    base = signature_base(message, (("@query", {}),), {"created": 1})
    assert base.startswith(b'"@query": ?\n')


def test_a_repeated_query_param_is_refused_rather_than_guessed():
    message = RequestMessage(
        method="GET", scheme="https", authority="h", path="/", query=b"a=1&a=2"
    )
    with pytest.raises(SignatureError, match="absent or repeated"):
        signature_base(message, (("@query-param", {"name": "a"}),), {"created": 1})


def test_an_unknown_component_parameter_is_refused_not_ignored():
    """`;sf` changes what a header canonicalizes to.

    Ignoring it would mean verifying a different string from the one signed,
    which is the shape of an accepted forgery.
    """
    message = RequestMessage(
        method="GET", scheme="https", authority="h", path="/",
        headers={b"x": b"1"},
    )
    with pytest.raises(SignatureError, match="unsupported component parameter"):
        signature_base(message, (("x", {"sf": True}),), {"created": 1})


def test_a_covered_header_that_is_absent_is_refused():
    message = RequestMessage(method="GET", scheme="https", authority="h", path="/")
    with pytest.raises(SignatureError, match="not present"):
        signature_base(message, (("x-missing", {}),), {"created": 1})


def test_a_component_covered_twice_is_refused():
    message = RequestMessage(method="GET", scheme="https", authority="h", path="/")
    with pytest.raises(SignatureError, match="covered twice"):
        signature_base(message, (("@method", {}), ("@method", {})), {"created": 1})


def test_mixed_case_component_identifiers_are_refused():
    message = RequestMessage(
        method="GET", scheme="https", authority="h", path="/", headers={b"x": b"1"}
    )
    with pytest.raises(SignatureError, match="lowercase"):
        signature_base(message, (("X", {}),), {"created": 1})


def test_covering_nothing_is_refused():
    message = RequestMessage(method="GET", scheme="https", authority="h", path="/")
    with pytest.raises(SignatureError, match="no components"):
        signature_base(message, (), {"created": 1})


# --- the structured-field parser --------------------------------------------
#
# Both signature headers are parsed before anything is verified, from bytes an
# unauthenticated caller chose. Mutation testing found this whole error surface
# untested: every refusal below could be deleted without a test objecting.


def parse_input(text: str):
    from wreath.signatures import _parse_dictionary

    return _parse_dictionary(text, inner_list=True)


def parse_signature(text: str):
    from wreath.signatures import _parse_dictionary

    return _parse_dictionary(text, inner_list=False)


def test_string_parser_requires_an_opening_quote() -> None:
    from wreath.signatures import _parse_string

    with pytest.raises(SignatureError, match="expected a quoted string"):
        _parse_string("not-quoted", 0)


def test_a_well_formed_signature_input_parses():
    components, params = parse_input(
        'sig1=("@method" "@path");created=1;keyid="k";alg="ed25519"'
    )["sig1"]
    assert components == (("@method", {}), ("@path", {}))
    assert params == {"created": 1, "keyid": "k", "alg": "ed25519"}


def test_a_byte_sequence_value_parses():
    value, _ = parse_signature("sig1=:AQID:")["sig1"]
    assert value == b"\x01\x02\x03"


@pytest.mark.parametrize(
    "text,message",
    [
        ('sig1=("@method"', "unterminated inner list"),
        ('sig1=("@method) ', "unterminated string"),
        ('sig1=("@method"),', "trailing comma"),
        ('sig1=("@method") sig2=("@path")', "expected a comma"),
        ('sig1=("@method");created=1;keyid="k', "unterminated string"),
        ("sig1=@method", "expected an inner list"),
    ],
)
def test_malformed_signature_input_is_refused(text, message):
    with pytest.raises(SignatureError, match=message):
        parse_input(text)


@pytest.mark.parametrize(
    "text,message",
    [
        ("sig1=:AQID", "unterminated byte sequence"),
        ("sig1=:not base64!:", "bad base64"),
        ("sig1=?2", "bad boolean"),
        ("sig1=1.2.3", "bad number"),
    ],
)
def test_malformed_signature_values_are_refused(text, message):
    with pytest.raises(SignatureError, match=message):
        parse_signature(text)


def test_a_bad_string_escape_is_refused():
    with pytest.raises(SignatureError, match="bad escape"):
        parse_input(r'sig1=("@method\n")')


def test_a_control_character_in_a_string_is_refused():
    with pytest.raises(SignatureError, match="bad character in string"):
        parse_input('sig1=("@met\x01hod")')


def test_an_oversized_header_is_refused_before_it_is_parsed():
    """The parse is bounded by something other than good intentions."""
    with pytest.raises(SignatureError, match="too large"):
        parse_input("sig1=(" + '"@method" ' * 5000 + ")")


def test_too_many_covered_components_are_refused():
    with pytest.raises(SignatureError, match="too many components"):
        parse_input("sig1=(" + '"@method" ' * 100 + ")")


def test_a_repeated_parameter_takes_the_last_value():
    """RFC 8941's rule, asserted rather than assumed."""
    _, params = parse_input('sig1=("@method");created=1;created=2')["sig1"]
    assert params["created"] == 2


def test_a_valueless_parameter_is_true():
    _, params = parse_input('sig1=("@method");sf')["sig1"]
    assert params["sf"] is True


def test_a_bare_key_with_no_value_parses_as_an_empty_list():
    components, _ = parse_input("sig1")["sig1"]
    assert components == ()


def test_request_response_binding_is_refused_by_name():
    """`;req` covers a component of the *response's* request. Not supported,
    and refused rather than silently treated as an ordinary component."""
    message = RequestMessage(
        method="GET", scheme="https", authority="h", path="/", headers={b"x": b"1"}
    )
    with pytest.raises(SignatureError, match="request-response binding"):
        signature_base(message, (("x", {"req": True}),), {"created": 1})


def test_an_unknown_derived_component_is_refused():
    message = RequestMessage(method="GET", scheme="https", authority="h", path="/")
    with pytest.raises(SignatureError, match="unsupported derived component"):
        signature_base(message, (("@nonsense", {}),), {"created": 1})


def test_query_param_without_a_name_is_refused():
    message = RequestMessage(
        method="GET", scheme="https", authority="h", path="/", query=b"a=1"
    )
    with pytest.raises(SignatureError, match="requires a name parameter"):
        signature_base(message, (("@query-param", {}),), {"created": 1})


def test_a_request_with_no_authority_cannot_cover_one():
    """An empty `@authority` would let a signature minted for one host verify
    against any other host that also sent no Host header."""
    message = RequestMessage(method="GET", scheme="https", authority="", path="/")
    with pytest.raises(SignatureError, match="no authority to cover"):
        signature_base(message, (("@authority", {}),), {"created": 1})


def test_the_default_port_is_dropped_from_the_authority():
    def authority(value: str, scheme: str) -> str:
        message = RequestMessage(
            method="GET", scheme=scheme, authority=value, path="/"
        )
        base = signature_base(message, (("@authority", {}),), {"created": 1})
        return base.split(b"\n")[0].split(b": ", 1)[1].decode()

    assert authority("Example.COM:443", "https") == "example.com"
    assert authority("example.com:80", "http") == "example.com"
    assert authority("example.com:8443", "https") == "example.com:8443"
