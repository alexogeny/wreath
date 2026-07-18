from __future__ import annotations

import pytest

from wreath._native import _core
from wreath._pure import webpolicy as pure


def test_native_webpolicy_exports_when_core_is_built() -> None:
    if _core is not None:
        assert hasattr(_core, "select_content_encoding")
        assert hasattr(_core, "append_vary")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"gzip", "gzip"),
        (b"br, gzip;q=0.5", "gzip"),
        (b"gzip;q=0", None),
        (b"*;q=1", "gzip"),
        (b"gzip;q=0, *;q=1", None),
        (b"identity", None),
        (b"gzip;q=bogus", None),
        (b"GZip ; q=1.000", "gzip"),
    ],
)
def test_encoding_selection(value: bytes, expected: str | None) -> None:
    assert pure.select_content_encoding(value) == expected
    if _core is not None and hasattr(_core, "select_content_encoding"):
        assert _core.select_content_encoding(value) == expected


def test_policy_helpers_native_pure_parity() -> None:
    assert pure.is_compressible_content_type(b"application/vnd.api+json; charset=utf-8")
    assert not pure.is_compressible_content_type(b"image/png")
    assert pure.cache_control_flags(b"private, no-transform") == pure.PRIVATE | pure.NO_TRANSFORM
    allowed = (b"https://example.test", b"https://[::1]:8443")
    vectors = (
        (b"https://EXAMPLE.test:443/", True),
        (b"https://example.test.evil", False),
        (b"https://[::1]:8443", True),
        (b"null", False),
    )
    for origin, expected in vectors:
        assert pure.origin_matches(origin, allowed) is expected
        if _core is not None and hasattr(_core, "origin_matches"):
            assert _core.origin_matches(origin, allowed) is expected


def test_header_mutations() -> None:
    headers = [(b"content-length", b"99"), (b"vary", b"Origin"), (b"vary", b"Cookie")]
    pure.replace_content_length(headers, 12)
    pure.append_vary(headers, b"Accept-Encoding")
    assert headers == [
        (b"vary", b"origin, cookie, accept-encoding"),
        (b"content-length", b"12"),
    ]
    if _core is not None and hasattr(_core, "append_vary"):
        native_headers = [
            (b"content-length", b"99"),
            (b"vary", b"Origin"),
            (b"vary", b"Cookie"),
        ]
        _core.replace_content_length(native_headers, 12)
        _core.append_vary(native_headers, b"Accept-Encoding")
        assert native_headers == headers


def test_append_missing_headers_native_pure_parity() -> None:
    additions = ((b"x-frame-options", b"DENY"), (b"x-content-type-options", b"nosniff"))
    pure_headers = [(b"X-Frame-Options", b"SAMEORIGIN")]
    pure.append_missing_headers(pure_headers, additions)
    assert pure_headers == [
        (b"X-Frame-Options", b"SAMEORIGIN"),
        (b"x-content-type-options", b"nosniff"),
    ]
    if _core is not None:
        native_headers = [(b"X-Frame-Options", b"SAMEORIGIN")]
        _core.append_missing_headers(native_headers, additions)
        assert native_headers == pure_headers

def test_append_missing_headers_duplicate_and_large_input_parity() -> None:
    cases = [
        ([], ()),
        ([(b"X-Test", b"existing")], ((b"x-test", b"new"),)),
        ([], ((b"X-Test", b"first"), (b"x-TEST", b"second"))),
        ([(b"x-dup", b"one"), (b"X-Dup", b"two")], ((b"other", b"value"),)),
        ([(b"X-\xff", b"existing")], ((b"x-\xff", b"new"),)),
    ]
    for initial, additions in cases:
        pure_headers = initial.copy()
        pure.append_missing_headers(pure_headers, additions)
        if _core is not None:
            native_headers = initial.copy()
            _core.append_missing_headers(native_headers, list(additions))
            assert native_headers == pure_headers

    headers = [(f"x-existing-{i}".encode(), b"value") for i in range(64)]
    additions = tuple((f"x-added-{i}".encode(), b"first") for i in range(64))
    additions += tuple((name.upper(), b"second") for name, _ in additions)
    pure_headers = headers.copy()
    pure.append_missing_headers(pure_headers, additions)
    assert len(pure_headers) == 128
    if _core is not None:
        native_headers = headers.copy()
        _core.append_missing_headers(native_headers, additions)
        assert native_headers == pure_headers


def test_append_missing_headers_validation_is_atomic() -> None:
    invalid_additions = ((b"valid", b"value"), (b"invalid", "not-bytes"))
    for backend in (pure, _core):
        if backend is None:
            continue
        headers = [(b"existing", b"value")]
        with pytest.raises(TypeError, match="header additions must be two-item bytes tuples"):
            backend.append_missing_headers(headers, invalid_additions)
        assert headers == [(b"existing", b"value")]
