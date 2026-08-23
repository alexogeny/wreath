"""Browser response/request policy, held to RFC 9110 rather than to ourselves.

Every vector is an expectation from outside Wreath: an example lifted from RFC
9110 with its clause quoted beside it, or -- where the behaviour is Wreath's own
choice rather than the RFC's -- a value spelled out with the derivation written
down.
"""

from __future__ import annotations

import pytest

from wreath._native import _core


def test_every_helper_this_file_covers_is_exported() -> None:
    """A missing export would otherwise skip a whole block of vectors silently."""
    for name in (
        "select_content_encoding",
        "append_vary",
        "replace_response_header",
        "replace_cookie",
        "replace_server_timing",
        "is_compressible_content_type",
        "cache_control_flags",
        "origin_matches",
        "replace_content_length",
        "append_missing_headers",
    ):
        assert hasattr(_core, name), name


# -- Accept-Encoding, RFC 9110 §12.5.3 ----------------------------------------
#
# `select_content_encoding` answers one question: which coding, if any, should
# this response be encoded with. `None` means "send it uncoded", which is the
# `identity` coding the RFC says is "acceptable by default".


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The five field values RFC 9110 §12.5.3 gives as its own worked
        # examples, in the order the RFC lists them.
        #
        #   Accept-Encoding: compress, gzip
        #   Accept-Encoding:
        #   Accept-Encoding: *
        #   Accept-Encoding: compress;q=0.5, gzip;q=1.0
        #   Accept-Encoding: gzip;q=1.0, identity; q=0.5, *;q=0
        #
        # gzip is named with the default weight of 1, and Wreath offers gzip.
        (b"compress, gzip", "gzip"),
        # "An Accept-Encoding header field with a combined field value that is
        # empty implies that the user agent does not want any content coding in
        # response." (§12.5.3) -- so: no coding.
        (b"", None),
        # "The asterisk '*' symbol in an Accept-Encoding field matches any
        # available content coding not explicitly listed in the field."
        # (§12.5.3). Wreath narrows this deliberately; see the block below.
        (b"*", "gzip"),
        # compress is not offered, so the surviving acceptable coding is gzip.
        (b"compress;q=0.5, gzip;q=1.0", "gzip"),
        # `*;q=0` excludes everything unlisted, but gzip is listed at q=1 and
        # the more specific entry wins.
        (b"gzip;q=1.0, identity; q=0.5, *;q=0", "gzip"),
        #
        # -- the rules those examples exercise, one at a time -----------------
        #
        # A weight is optional and defaults to 1 (§12.4.2).
        (b"gzip", "gzip"),
        # "a value of 0 means 'not acceptable'" (§12.4.2).
        (b"gzip;q=0", None),
        # "the acceptable content coding with the highest non-zero qvalue is
        # preferred" -- br is not offered, gzip at 0.5 is.
        (b"br, gzip;q=0.5", "gzip"),
        # A wildcard covers a coding the client did not name (§12.5.3).
        (b"*;q=1", "gzip"),
        # Most specific match wins: the explicit `gzip;q=0` is not overridden by
        # the wildcard, because `*` "matches any available content coding **not
        # explicitly listed**" (§12.5.3).
        (b"gzip;q=0, *;q=1", None),
        # identity is what `None` means here: the client asked for no coding.
        (b"identity", None),
        # A malformed weight is not a weight. RFC 9110 §12.4.2 admits only
        # `0`/`1` with at most three fractional digits, so `bogus` leaves the
        # entry unusable and Wreath treats it as not acceptable rather than
        # guessing 1.
        (b"gzip;q=bogus", None),
        # "content coding values are case-insensitive" (§8.4.1), and OWS is
        # allowed around the parameter separators (§5.6.1.2, §12.4.2).
        (b"GZip ; q=1.000", "gzip"),
        #
        # -- Wreath's own policy, where the RFC leaves a choice ---------------
        #
        # zstd is offered only to a client that named it. A bare wildcard still
        # means gzip, so nothing that used to get gzip gets an unasked-for
        # coding. §12.5.3 would permit reading `*` as consent to zstd; a client
        # sending `*` is far likelier to be old than to be new, and the decoder
        # it lacks shows up as a corrupt body rather than as a 415.
        (b"*;q=0.5", "gzip"),
        (b"zstd", "zstd"),
        (b"ZStd ; q=1.000", "zstd"),
        (b"zstd;q=0", None),
        (b"zstd;q=bogus", None),
        (b"br, zstd;q=0.1", "zstd"),
        # Both acceptable: higher q wins, per §12.4.2. A tie goes to zstd, which
        # decodes faster and smaller at every level Wreath would pick.
        (b"gzip, zstd", "zstd"),
        (b"zstd, gzip", "zstd"),
        (b"gzip;q=1, zstd;q=0.5", "gzip"),
        (b"zstd;q=0.5, gzip;q=0.4", "zstd"),
        (b"gzip;q=0, zstd", "zstd"),
        (b"zstd;q=0, gzip", "gzip"),
        (b"gzip;q=0, zstd;q=0", None),
        (b"gzip;q=0, zstd;q=0, *;q=1", None),
    ],
)
def test_encoding_selection(value: bytes, expected: str | None) -> None:
    assert _core.select_content_encoding(value) == expected


@pytest.mark.parametrize(
    ("value", "available", "expected"),
    [
        (b"dcz, gzip, zstd", True, "dcz"),
        (b"dcz, gzip, zstd", False, "gzip"),
        (b"dcz;q=1, zstd;q=1, gzip;q=0.5", False, "zstd"),
        (b"dcz;q=0.5, gzip, zstd", True, "zstd"),
        (b"dcz", False, None),
        (b"dcz;q=bogus, gzip", True, "gzip"),
    ],
)
def test_prepared_encoding_selection_is_one_native_parse(
    value: bytes, available: bool, expected: str | None
) -> None:
    assert _core.select_prepared_content_encoding(value, available) == expected


def test_policy_helpers_follow_the_rfcs_they_cite() -> None:
    # A structured suffix with a `+json` subtype is JSON for every purpose that
    # matters here (RFC 6839 §3.1), and the `charset` parameter is not part of
    # the media type (RFC 9110 §8.3.1).
    assert _core.is_compressible_content_type(b"application/vnd.api+json; charset=utf-8")
    assert not _core.is_compressible_content_type(b"image/png")
    # Cache-Control directive names are case-insensitive and comma-separated
    # (RFC 9111 §5.2); `private` and `no-transform` are response directives
    # (RFC 9111 §5.2.2). The numeric flags are an ABI rather than a spec, so
    # they are written out here: a caller stores them, and renumbering would
    # change what a stored flag word means.
    assert (_core.NO_TRANSFORM, _core.NO_STORE, _core.PRIVATE, _core.PUBLIC) == (1, 2, 4, 8)
    assert _core.cache_control_flags(b"private, no-transform") == 4 | 1
    allowed = (b"https://example.test", b"https://[::1]:8443")
    vectors = (
        # RFC 6454 §6.2 serializes an origin as scheme "://" host, appending
        # ":" port only when the port differs from the scheme's default -- and
        # §4 lower-cases scheme and host. So `https://EXAMPLE.test:443/` and
        # `https://example.test` are the same origin.
        (b"https://EXAMPLE.test:443/", True),
        # Origin comparison is on the whole triple, not a suffix.
        (b"https://example.test.evil", False),
        # An IPv6 literal keeps its brackets (RFC 3986 §3.2.2) and 8443 is not
        # https' default, so the port stays in the serialization.
        (b"https://[::1]:8443", True),
        # "null" is the serialization of an opaque origin (RFC 6454 §6.2) and
        # matches nothing on an allow-list of real origins.
        (b"null", False),
    )
    for origin, expected in vectors:
        assert _core.origin_matches(origin, allowed) is expected


def test_allowed_origins_are_normalized_before_comparison() -> None:
    # The allow-list entry, not the request's Origin, is the one carrying the
    # default port and the trailing slash -- RFC 6454 §6.2 normalizes both away.
    allowed = (b"https://EXAMPLE.test:443/",)
    origin = b"https://example.test"
    assert _core.origin_matches(origin, allowed)


# -- Vary, RFC 9110 §12.5.5 ---------------------------------------------------


def test_header_mutations() -> None:
    """Two Vary field lines collapse into one, and Content-Length is replaced.

    RFC 9110 §5.3 permits a recipient to combine field lines with the same name
    "into one field line ... by appending each subsequent field line value to
    the initial field line value in order, separated by a comma", which is what
    `append_vary` does before adding its own token. §12.5.5 makes the value a
    list of field names, and §5.1 makes those names case-insensitive, so
    `Origin` and `Cookie` normalize to lower case.
    """
    expected = [
        (b"vary", b"origin, cookie, accept-encoding"),
        (b"content-length", b"12"),
    ]
    initial = [(b"content-length", b"99"), (b"vary", b"Origin"), (b"vary", b"Cookie")]

    headers = initial.copy()
    _core.replace_content_length(headers, 12)
    _core.append_vary(headers, b"Accept-Encoding")
    assert headers == expected


@pytest.mark.parametrize(
    ("initial", "token", "expected"),
    [
        # No Vary yet: the token becomes the whole field value.
        ([], b"Accept-Encoding", [(b"vary", b"accept-encoding")]),
        # "A Vary field value of '*' signals that anything about the request
        # might play a role in selecting the response representation"
        # (§12.5.5), so it already subsumes any token being added and adding
        # one must not narrow it back to a list.
        ([(b"Vary", b"*")], b"Accept-Encoding", [(b"vary", b"*")]),
        # ... including when `*` arrives alongside named fields.
        ([(b"Vary", b"Origin, *")], b"Accept-Encoding", [(b"vary", b"*")]),
        # "Field names are case-insensitive" (§5.1), so this is one field name
        # listed twice across two field lines, not two.
        (
            [(b"Vary", b"Accept-Encoding"), (b"vary", b"ACCEPT-ENCODING")],
            b"accept-encoding",
            [(b"vary", b"accept-encoding")],
        ),
        # The merged line takes the position of the first Vary line; unrelated
        # headers keep their order (§5.3 preserves order within a field name,
        # and imposes none across names).
        (
            [(b"content-type", b"text/plain"), (b"Vary", b"Origin")],
            b"Accept-Encoding",
            [(b"content-type", b"text/plain"), (b"vary", b"origin, accept-encoding")],
        ),
    ],
)
def test_append_vary_follows_rfc_9110(
    initial: list[tuple[bytes, bytes]],
    token: bytes,
    expected: list[tuple[bytes, bytes]],
) -> None:
    headers = initial.copy()
    _core.append_vary(headers, token)
    assert headers == expected


def test_each_replacement_helper_replaces_exactly_its_own_field() -> None:
    """One replacement each, with every other header of that name preserved.

    The three helpers differ in what "one" means, and the expected list below is
    what distinguishes them: `replace_response_header` drops every field line
    with that name (§5.3 makes them one field, so replacing means replacing all
    of it), `replace_cookie` keeps the Set-Cookie lines whose cookie-name
    differs (RFC 6265 §4.1 makes each line a separate cookie), and
    `replace_server_timing` keeps the other metrics inside the field value
    (Server-Timing is a list, and its metric names are case-insensitive).
    Each replacement lands at the end, where the first surviving header of that
    name used to be irrelevant.
    """
    initial = [
        (b"X-Request-ID", b"old"),
        (b"set-cookie", b"session=keep; Path=/"),
        (b"set-cookie", b"wreath_csrf=old; Path=/"),
        (b"server-timing", b"db;dur=2, total;dur=9"),
        (b"server-timing", b"cache;dur=1"),
        (b"x-request-id", b"duplicate"),
    ]
    expected = [
        (b"set-cookie", b"session=keep; Path=/"),
        (b"x-request-id", b"new"),
        (b"set-cookie", b"wreath_csrf=new; Path=/"),
        (b"server-timing", b"db;dur=2, cache;dur=1, total;dur=3"),
    ]

    headers = initial.copy()
    _core.replace_response_header(headers, b"x-request-id", b"new")
    _core.replace_cookie(headers, b"wreath_csrf=", b"wreath_csrf=new; Path=/")
    _core.replace_server_timing(headers, b"total", b"total;dur=3")
    assert headers == expected


def test_append_missing_headers_skips_a_name_already_present() -> None:
    # `X-Frame-Options` is already present under a different case, and RFC 9110
    # §5.1 makes that the same field, so only the second addition lands.
    additions = ((b"x-frame-options", b"DENY"), (b"x-content-type-options", b"nosniff"))
    expected = [
        (b"X-Frame-Options", b"SAMEORIGIN"),
        (b"x-content-type-options", b"nosniff"),
    ]

    headers = [(b"X-Frame-Options", b"SAMEORIGIN")]
    _core.append_missing_headers(headers, additions)
    assert headers == expected


@pytest.mark.parametrize(
    ("initial", "additions", "expected"),
    [
        # Nothing to do, and no header invented.
        ([], (), []),
        # Present already, under a different case (RFC 9110 §5.1).
        (
            [(b"X-Test", b"existing")],
            ((b"x-test", b"new"),),
            [(b"X-Test", b"existing")],
        ),
        # Two additions naming the same field: the first wins and the second is
        # a duplicate of it, again case-insensitively.
        (
            [],
            ((b"X-Test", b"first"), (b"x-TEST", b"second")),
            [(b"X-Test", b"first")],
        ),
        # An already-duplicated field is left exactly as it was found; this
        # helper adds, it does not merge.
        (
            [(b"x-dup", b"one"), (b"X-Dup", b"two")],
            ((b"other", b"value"),),
            [(b"x-dup", b"one"), (b"X-Dup", b"two"), (b"other", b"value")],
        ),
        # A non-ASCII byte in a field name is not a letter and has no case, so
        # `0xFF` compares equal to itself and the field counts as present. The
        # fold must be ASCII-only (RFC 9110 §5.1 names are ASCII); a
        # locale-sensitive `tolower` could map 0xFF and lose the match.
        (
            [(b"X-\xff", b"existing")],
            ((b"x-\xff", b"new"),),
            [(b"X-\xff", b"existing")],
        ),
    ],
)
def test_append_missing_headers_duplicates(
    initial: list[tuple[bytes, bytes]],
    additions: tuple[tuple[bytes, bytes], ...],
    expected: list[tuple[bytes, bytes]],
) -> None:
    headers = initial.copy()
    _core.append_missing_headers(headers, list(additions))
    assert headers == expected


def test_append_missing_headers_crosses_the_set_threshold() -> None:
    """Past 256 name comparisons `append_missing_headers` swaps its scan for a set.

    The answer may not change with it, so the expected list is written out in
    full: 64 existing headers untouched, the 64 lower-case additions appended in
    order, and the 64 upper-case repeats of those recognised as the same field
    names (RFC 9110 §5.1) and dropped.
    """
    headers = [(f"x-existing-{i}".encode(), b"value") for i in range(64)]
    additions = tuple((f"x-added-{i}".encode(), b"first") for i in range(64))
    additions += tuple((name.upper(), b"second") for name, _ in additions)
    expected = headers + [(f"x-added-{i}".encode(), b"first") for i in range(64)]

    subject = headers.copy()
    _core.append_missing_headers(subject, additions)
    assert subject == expected


def test_append_missing_headers_validation_is_atomic() -> None:
    invalid_additions = ((b"valid", b"value"), (b"invalid", "not-bytes"))
    headers = [(b"existing", b"value")]
    with pytest.raises(TypeError, match="header additions must be two-item bytes tuples"):
        _core.append_missing_headers(headers, invalid_additions)
    # The valid addition ahead of the bad one must not have landed: a partial
    # apply leaves a response carrying half a security-header set.
    assert headers == [(b"existing", b"value")]
