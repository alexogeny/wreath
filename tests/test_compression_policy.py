from __future__ import annotations

import gzip
import zlib
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import JSONResponse, Wreath
from wreath._compression import (
    _dcz_compress,
    _dcz_decompress,
    _prepare_dcz_dictionary,
    _RenderedFragments,
)
from wreath._native import _core
from wreath.cache_control import CacheControl
from wreath.compression import (
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    gzip_compress,
)
from wreath.policy import (
    CachePolicy,
    CompressionPolicy,
    CsrfPolicy,
    HttpPolicy,
    SecurityHeadersPolicy,
)
from wreath.response import Response, StreamingResponse
from wreath.templates import Template
from wreath.testing import TestClient


def test_gzip_facade_round_trip_and_state() -> None:
    payload = b"wreath" * 10_000
    assert zlib.decompress(gzip_compress(payload), wbits=31) == payload
    compressor = GzipCompressor()
    encoded = compressor.compress(payload[:100]) + compressor.compress(payload[100:])
    encoded += compressor.finish()
    assert zlib.decompress(encoded, wbits=31) == payload
    with pytest.raises(RuntimeError):
        compressor.finish()


def test_dcz_workspace_lease_is_safe_across_request_threads() -> None:
    dictionary = b'<li data-kind="incident">wreath</li>' * 128
    prepared = _prepare_dcz_dictionary(dictionary)
    documents = [
        _RenderedFragments(f'<main data-id="{index}">'.encode(), dictionary) for index in range(32)
    ]

    with ThreadPoolExecutor(max_workers=8) as workers:
        encoded = tuple(workers.map(lambda body: _dcz_compress(prepared, body, 3), documents))

    assert tuple(
        _dcz_decompress(body, dictionary, max_output_bytes=100_000) for body in encoded
    ) == tuple(document.materialize() for document in documents)


@pytest.mark.parametrize(
    ("prefix", "tail", "level"),
    [
        (b"", b"x", 1),
        (b"prefix", b"", 3),
        (b"p" * 257, b"tail" * 16_385, 9),
    ],
)
def test_dcz_fragment_workspace_matches_the_materialized_stream(
    prefix: bytes, tail: bytes, level: int
) -> None:
    dictionary = (b"dictionary-row:" + tail[-128:]) * 64
    prepared = _prepare_dcz_dictionary(dictionary)
    fragments = _RenderedFragments(prefix, tail)

    encoded = _dcz_compress(prepared, fragments, level)

    assert (
        _dcz_decompress(
            encoded,
            dictionary,
            max_output_bytes=max(1, len(prefix) + len(tail)),
        )
        == prefix + tail
    )


def test_dcz_workspace_rebuilds_its_dictionary_when_the_level_changes() -> None:
    dictionary = b'<li data-kind="incident">wreath</li>' * 128
    prepared = _prepare_dcz_dictionary(dictionary)
    document = b"<main>" + dictionary + b"</main>"

    encoded = tuple(_dcz_compress(prepared, document, level) for level in (1, 9, 3))

    assert tuple(
        _dcz_decompress(body, dictionary, max_output_bytes=len(document)) for body in encoded
    ) == (document, document, document)


def test_dcz_workspace_falls_back_to_the_python_codec_when_libzstd_is_unavailable() -> None:
    dictionary = b'<li data-kind="incident">wreath</li>' * 128
    token, digest, prepared_dictionary, _workspace = _prepare_dcz_dictionary(dictionary)
    prepared = (token, digest, prepared_dictionary, None)
    document = _RenderedFragments(b'<main data-id="42">', dictionary)

    encoded = _dcz_compress(prepared, document, 3)

    assert _dcz_decompress(encoded, dictionary, max_output_bytes=100_000) == (
        document.materialize()
    )
    plain = b'<main data-id="43">' + dictionary
    encoded_plain = _dcz_compress(prepared, plain, 3)
    assert _dcz_decompress(encoded_plain, dictionary, max_output_bytes=100_000) == plain


def test_dcz_workspace_uses_the_native_encoder_when_one_was_prepared(monkeypatch) -> None:
    dictionary = b'<li data-kind="incident">wreath</li>' * 128
    token, digest, prepared_dictionary, _workspace = _prepare_dcz_dictionary(dictionary)
    workspace = object()
    monkeypatch.setattr(
        _core,
        "dcz_compress_with",
        lambda actual, actual_digest, body, level: (
            b"native"
            if (actual, actual_digest, body, level) == (workspace, digest, b"body", 3)
            else b"wrong"
        ),
    )

    assert _dcz_compress((token, digest, prepared_dictionary, workspace), b"body", 3) == (b"native")


@pytest.mark.asyncio
async def test_prepared_compression_ladder_is_dcz_fragment_then_format_gzip() -> None:
    prefix = b'{"request":"00000000","items":['
    stable = b'{"id":7,"name":"wreath"},' * 128
    suffix = b'{"request":"00000000"}]}'
    document = prefix + stable + suffix
    dictionary = b'{"id":0,"name":"wreath"},' * 128
    policy = CompressionPolicy(minimum_size=0)
    token = policy._configure_dcz_dictionary("application/json", dictionary)
    policy._configure_gzip_fragment(
        "application/json",
        document,
        prefix_bytes=len(prefix),
        suffix_bytes=len(suffix),
    )

    def request(available: bytes, accepted: bytes = b"dcz, gzip, zstd"):
        headers = {
            b"accept-encoding": accepted,
            b"available-dictionary": available,
        }
        return SimpleNamespace(
            method="GET",
            identity=None,
            scheme="https",
            _header_bytes=headers.get,
        )

    exact = Response(document, media_type=b"application/json")
    await policy.after(request(token), exact)
    assert dict(exact.headers)[b"content-encoding"] == b"dcz"
    assert _dcz_decompress(exact.body, dictionary, max_output_bytes=100_000) == document

    fragment = Response(document, media_type=b"application/json")
    await policy.after(request(b":wrong:"), fragment)
    assert dict(fragment.headers)[b"content-encoding"] == b"gzip"
    assert gzip.decompress(fragment.body) == document
    first_member = zlib.decompressobj(wbits=31)
    first_member.decompress(fragment.body)
    assert first_member.unused_data.startswith(b"\x1f\x8b")

    prepared_body = policy._gzip_fragment_body("application/json", prefix, suffix)
    assert type(prepared_body) is bytes
    assert prepared_body == document
    prepared = Response(prepared_body, media_type=b"application/json")
    await policy.after(request(b":wrong:"), prepared)
    assert gzip.decompress(prepared.body) == document
    first_member = zlib.decompressobj(wbits=31)
    assert first_member.decompress(prepared.body) == prefix
    assert first_member.unused_data.startswith(b"\x1f\x8b")

    changed = prefix + stable[:-1] + b"!" + suffix
    format_gzip = Response(changed, media_type=b"application/json")
    await policy.after(request(b":wrong:"), format_gzip)
    assert dict(format_gzip.headers)[b"content-encoding"] == b"gzip"
    assert zlib.decompress(format_gzip.body, wbits=31) == changed
    one_member = zlib.decompressobj(wbits=31)
    one_member.decompress(format_gzip.body)
    assert one_member.eof and one_member.unused_data == b""

    zstd_preferred = Response(document, media_type=b"application/json")
    await policy.after(
        request(b":wrong:", b"dcz;q=1, zstd;q=1, gzip;q=0.5"),
        zstd_preferred,
    )
    assert dict(zstd_preferred.headers)[b"content-encoding"] == b"zstd"
    assert zstd.decompress(zstd_preferred.body) == document

    malformed_dcz = Response(document, media_type=b"application/json")
    await policy.after(request(token, b"dcz;q=1e0, gzip"), malformed_dcz)
    assert dict(malformed_dcz.headers)[b"content-encoding"] == b"gzip"

    lower_dcz = Response(document, media_type=b"application/json")
    await policy.after(request(token, b"dcz;q=0.5, gzip, zstd"), lower_dcz)
    assert dict(lower_dcz.headers)[b"content-encoding"] == b"zstd"

    unavailable_only = Response(document, media_type=b"application/json")
    await policy.after(request(b":wrong:", b"dcz"), unavailable_only)
    assert b"content-encoding" not in dict(unavailable_only.headers)
    assert unavailable_only.body == document


def test_prepared_fragment_can_render_dynamic_prefix_into_final_body() -> None:
    template = Template.from_string("<h1>{{ title }}</h1>")
    prefix = b"<h1>Wreath &amp; Co</h1>"
    stable = b"<main>stable</main>"
    policy = CompressionPolicy(minimum_size=0)
    policy._configure_gzip_fragment(
        "html",
        prefix + stable,
        prefix_bytes=len(prefix),
        suffix_bytes=0,
    )

    body = policy._gzip_fragment_render("html", template, {"title": "Wreath & Co"})

    assert body == prefix + stable


@pytest.mark.asyncio
async def test_dcz_fragment_render_keeps_complete_bytes_for_non_dcz_observers() -> None:
    template = Template.from_string("<h1>{{ title }}</h1>")
    prefix = b"<h1>Wreath &amp; Co</h1>"
    stable = b"<main>stable</main>" * 1_000
    dictionary = b"<h1>Neighbour</h1>" + stable
    policy = CompressionPolicy(minimum_size=0)
    token = policy._configure_dcz_dictionary("html", dictionary)
    policy._configure_gzip_fragment(
        "html", prefix + stable, prefix_bytes=len(prefix), suffix_bytes=0
    )
    headers = {b"accept-encoding": b"dcz, gzip", b"available-dictionary": token}
    request = SimpleNamespace(
        method="GET",
        identity=None,
        scheme="https",
        _header_bytes=headers.get,
    )

    body = policy._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})

    assert bytes(body) == prefix + stable
    assert body == prefix + stable
    assert body[:] == prefix + stable
    assert memoryview(body).tobytes() == prefix + stable
    assert len(body) == len(prefix) + len(stable)
    response = Response(body, media_type=b"text/html")
    await policy.after(request, response)
    assert _dcz_decompress(response.body, dictionary, max_output_bytes=len(prefix + stable)) == (
        prefix + stable
    )


def test_dcz_fragment_render_requires_every_request_and_preparation_condition() -> None:
    template = Template.from_string("<h1>{{ title }}</h1>")
    prefix = b"<h1>Wreath &amp; Co</h1>"
    stable = b"<main>stable</main>"
    dictionary = b"<h1>Neighbour</h1>" + stable

    def configured(*, with_dictionary: bool = True, with_fragment: bool = True):
        policy = CompressionPolicy(minimum_size=0)
        token = (
            policy._configure_dcz_dictionary("html", dictionary)
            if with_dictionary
            else b":missing:"
        )
        if with_fragment:
            policy._configure_gzip_fragment(
                "html", prefix + stable, prefix_bytes=len(prefix), suffix_bytes=0
            )
        return policy, token

    cases = []
    policy, token = configured(with_dictionary=False)
    cases.append((policy, "https", None, b"dcz, gzip", token, prefix + stable))
    policy, token = configured()
    cases.extend(
        (
            (policy, "http", None, b"dcz, gzip", token, prefix + stable),
            (policy, "https", None, None, token, prefix + stable),
            (policy, "https", None, b"dcz, gzip", b":wrong:", prefix + stable),
            (policy, "https", None, b"gzip", token, prefix + stable),
        )
    )
    private, token = configured()
    private.compress_authenticated = False
    cases.append((private, "https", object(), b"dcz, gzip", token, prefix + stable))

    for policy, scheme, identity, accepted, available, expected in cases:
        headers = {b"accept-encoding": accepted, b"available-dictionary": available}
        request = SimpleNamespace(
            identity=identity,
            scheme=scheme,
            _header_bytes=headers.get,
        )
        body = policy._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})
        assert type(body) is bytes
        assert body == expected

    authenticated, token = configured()
    authenticated.compress_authenticated = True
    headers = {b"accept-encoding": b"dcz, gzip", b"available-dictionary": token}
    request = SimpleNamespace(
        identity=object(),
        scheme="https",
        _header_bytes=headers.get,
    )
    body = authenticated._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})
    assert type(body) is _RenderedFragments
    assert bytes(body) == prefix + stable
    assert body.prefix == prefix
    assert body.tail == stable

    policy, token = configured(with_fragment=False)
    headers = {b"accept-encoding": b"dcz, gzip", b"available-dictionary": token}
    request = SimpleNamespace(
        identity=None,
        scheme="https",
        _header_bytes=headers.get,
    )
    with pytest.raises(RuntimeError, match="gzip fragment format 'html' is not prepared"):
        policy._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})


def test_dcz_fragment_render_refuses_stale_or_unfusable_preparation() -> None:
    template = Template.from_string("<h1>{{ title }}</h1>")
    prefix = b"<h1>Wreath &amp; Co</h1>"
    stable = b"<main>stable</main>"
    dictionary = b"<h1>Neighbour</h1>" + stable
    policy = CompressionPolicy(minimum_size=0)
    token = policy._configure_dcz_dictionary("html", dictionary)
    headers = {b"accept-encoding": b"dcz, gzip", b"available-dictionary": token}
    request = SimpleNamespace(
        identity=None,
        scheme="https",
        _header_bytes=headers.get,
    )

    policy._configure_gzip_fragment(
        "html", prefix + stable, prefix_bytes=len(prefix), suffix_bytes=0
    )
    policy.gzip_level += 1
    with pytest.raises(RuntimeError, match="no longer matches"):
        policy._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})

    policy.gzip_level -= 1
    policy._configure_gzip_fragment(
        "html", prefix + stable + b"!", prefix_bytes=len(prefix), suffix_bytes=1
    )
    with pytest.raises(ValueError, match="zero-byte suffix"):
        policy._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})

    policy._configure_gzip_fragment(
        "html", prefix + stable, prefix_bytes=len(prefix) + 1, suffix_bytes=0
    )
    with pytest.raises(ValueError, match=f"exactly {len(prefix) + 1} bytes"):
        policy._dcz_fragment_render(request, "html", template, {"title": "Wreath & Co"})


@pytest.mark.asyncio
async def test_zstd_materializes_a_fragment_body_before_compressing() -> None:
    prefix = b'<main data-id="42">'
    tail = b"<section>stable</section>" * 128
    request = SimpleNamespace(
        method="GET",
        identity=None,
        scheme="https",
        _header_bytes={b"accept-encoding": b"zstd"}.get,
    )
    response = Response(_RenderedFragments(prefix, tail), media_type=b"text/html")

    await CompressionPolicy(minimum_size=0).after(request, response)

    assert zstd.decompress(response.body) == prefix + tail


@pytest.mark.parametrize(
    ("status", "headers", "media_type"),
    [
        (206, (), b"text/html"),
        (200, ((b"content-encoding", b"identity"),), b"text/html"),
        (200, ((b"content-range", b"bytes 0-1/2"),), b"text/html"),
        (200, ((b"cache-control", b"no-transform"),), b"text/html"),
        (200, (), b"image/png"),
    ],
)
@pytest.mark.asyncio
async def test_fragment_body_remains_complete_across_compression_early_returns(
    status: int,
    headers: tuple[tuple[bytes, bytes], ...],
    media_type: bytes,
) -> None:
    prefix = b'<main data-id="42">'
    tail = b"<section>stable</section>" * 128
    request = SimpleNamespace(
        method="GET",
        identity=None,
        scheme="https",
        _header_bytes={b"accept-encoding": b"dcz, gzip"}.get,
    )
    response = Response(
        _RenderedFragments(prefix, tail),
        status=status,
        headers=headers,
        media_type=media_type,
    )

    await CompressionPolicy(minimum_size=0).after(request, response)

    assert bytes(response.body) == prefix + tail
    assert dict(response.headers)[b"content-length"] == str(len(prefix + tail)).encode()


@pytest.mark.asyncio
async def test_compression_policy_updates_headers_and_vary() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "compress me" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "br, gzip"})
    assert response.header("content-encoding") == "gzip"
    assert "accept-encoding" in (response.header("vary") or "")
    assert int(response.header("content-length") or "0") == len(response.body)
    assert b"compress me" in zlib.decompress(response.body, wbits=31)


@pytest.mark.asyncio
async def test_streaming_compression_does_not_gather_source() -> None:
    advanced: list[int] = []

    async def source():
        for index in range(3):
            advanced.append(index)
            yield (f"chunk-{index}" * 100).encode()

    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=0)))

    @app.get("/")
    async def index(request: Any) -> StreamingResponse:
        return StreamingResponse(source(), headers=[(b"content-type", b"text/plain")])

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "gzip"})
    assert advanced == [0, 1, 2]
    assert zlib.decompress(response.body, wbits=31).startswith(b"chunk-0")


@pytest.mark.asyncio
async def test_recommended_policy_order_sees_cookies_before_cache_and_compression() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            csrf=CsrfPolicy("s" * 32, secure=False),
            security_headers=SecurityHeadersPolicy(),
        )
    )
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))
    app.configure_http_policy(
        HttpPolicy(cache_control=CachePolicy(CacheControl(public=True, max_age=60)))
    )

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "ordered" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "gzip"})

    assert response.header("cache-control") == "private, no-store"
    assert response.header("content-encoding") == "gzip"
    vary = response.header("vary") or ""
    assert "accept-encoding" in vary
    assert response.header("set-cookie") is not None
    assert response.header("x-content-type-options") == "nosniff"


@pytest.mark.asyncio
async def test_zstd_is_served_to_a_client_that_names_it() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "compress me" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd, gzip"})
    assert response.header("content-encoding") == "zstd"
    assert "accept-encoding" in (response.header("vary") or "")
    assert int(response.header("content-length") or "0") == len(response.body)
    assert b"compress me" in zstd.decompress(response.body)


@pytest.mark.asyncio
async def test_bare_wildcard_still_means_gzip_not_zstd() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "wildcard" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "*"})
    assert response.header("content-encoding") == "gzip"
    assert zlib.decompress(response.body, wbits=31).startswith(b'{"message"')


@pytest.mark.asyncio
async def test_client_preference_for_gzip_is_honoured_over_zstd() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "preference" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "gzip;q=1.0, zstd;q=0.5"})
    assert response.header("content-encoding") == "gzip"


@pytest.mark.asyncio
async def test_each_coding_gets_its_own_etag() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "tagged" * 100})
        response.headers.append((b"etag", b'"v1"'))
        return response

    async with TestClient(app) as client:
        plain = await client.get("/", headers={"accept-encoding": "identity"})
        gzipped = await client.get("/", headers={"accept-encoding": "gzip"})
        zstded = await client.get("/", headers={"accept-encoding": "zstd"})

    assert plain.header("etag") == '"v1"'
    assert gzipped.header("etag") == '"v1--gzip"'
    assert zstded.header("etag") == '"v1--zstd"'
    assert len({plain.header("etag"), gzipped.header("etag"), zstded.header("etag")}) == 3


@pytest.mark.asyncio
async def test_weak_etag_keeps_its_prefix_under_zstd() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "weak" * 100})
        response.headers.append((b"etag", b'W/"v2"'))
        return response

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd"})
    assert response.header("etag") == 'W/"v2--zstd"'


@pytest.mark.asyncio
async def test_streaming_zstd_is_a_single_readable_frame() -> None:
    async def source():
        for index in range(4):
            yield (f"chunk-{index}" * 100).encode()

    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=0)))

    @app.get("/")
    async def index(request: Any) -> StreamingResponse:
        return StreamingResponse(source(), headers=[(b"content-type", b"text/plain")])

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd"})

    assert response.header("content-encoding") == "zstd"
    decoded = zstd.decompress(response.body)
    assert decoded.startswith(b"chunk-0")
    assert decoded.endswith(b"chunk-3" * 1)
    # One frame, not one per chunk: a second frame would decode fine here but
    # arrives as a trailing empty frame in the double-finish failure mode.
    assert response.body.count(b"\x28\xb5\x2f\xfd") == 1


def test_zstd_level_is_validated_against_libzstd() -> None:
    CompressionPolicy(zstd_level=ZSTD_MIN_LEVEL)
    CompressionPolicy(zstd_level=ZSTD_MAX_LEVEL)
    with pytest.raises(ValueError, match="zstd_level"):
        CompressionPolicy(zstd_level=ZSTD_MAX_LEVEL + 1)
    with pytest.raises(ValueError, match="zstd_level"):
        CompressionPolicy(zstd_level=ZSTD_MIN_LEVEL - 1)


def test_minimum_size_and_gzip_level_are_validated() -> None:
    CompressionPolicy(minimum_size=0, gzip_level=0)
    CompressionPolicy(gzip_level=9)
    with pytest.raises(ValueError, match="minimum_size"):
        CompressionPolicy(minimum_size=-1)
    for level in (-1, 10):
        with pytest.raises(ValueError, match="gzip_level"):
            CompressionPolicy(gzip_level=level)


@pytest.mark.asyncio
async def test_no_transform_and_incompressible_types_refuse_zstd_too() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/no-transform")
    async def no_transform(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "leave me" * 100})
        response.headers.append((b"cache-control", b"no-transform"))
        return response

    @app.get("/png")
    async def png(request: Any) -> Response:
        return Response(b"\x89PNG" + b"\x00" * 4000, media_type=b"image/png")

    async with TestClient(app) as client:
        kept = await client.get("/no-transform", headers={"accept-encoding": "zstd"})
        image = await client.get("/png", headers={"accept-encoding": "zstd"})

    assert kept.header("content-encoding") is None
    assert image.header("content-encoding") is None


@pytest.mark.asyncio
async def test_a_precomputed_integrity_field_prevents_a_later_content_coding() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/content")
    async def content(request: Any) -> Response:
        response = Response(b"content integrity" * 100, media_type=b"text/plain")
        response.set_content_digest("sha-256")
        return response

    @app.get("/representation")
    async def representation(request: Any) -> Response:
        response = Response(b"representation integrity" * 100, media_type=b"text/plain")
        response.set_repr_digest("sha-256")
        return response

    async with TestClient(app) as client:
        content_response = await client.get(
            "/content", headers={"accept-encoding": "gzip"}
        )
        representation_response = await client.get(
            "/representation", headers={"accept-encoding": "gzip"}
        )

    assert content_response.header("content-digest") is not None
    assert content_response.header("content-encoding") is None
    assert representation_response.header("repr-digest") is not None
    assert representation_response.header("content-encoding") is None


@pytest.mark.asyncio
async def test_the_etag_suffix_lands_on_the_etag_and_not_a_neighbour() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=16)))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "neighbours" * 100})
        response.headers.append((b"etag", b'"v3"'))
        response.headers.append((b"x-trailing", b"kept"))
        return response

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd"})

    assert response.header("etag") == '"v3--zstd"'
    assert response.header("content-type") == "application/json"
    assert response.header("x-trailing") == "kept"
    assert response.header("content-encoding") == "zstd"
    # Exactly one etag: a second would let a cache pick either body for one tag.
    assert sum(1 for name, _ in response.headers if name.lower() == b"etag") == 1


@pytest.mark.asyncio
async def test_a_file_response_is_served_uncompressed(tmp_path) -> None:
    from wreath.response import FileResponse

    served = tmp_path / "notes.txt"
    served.write_bytes(b"plain text worth compressing" * 100)

    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(compression=CompressionPolicy(minimum_size=0, compress_streaming=True))
    )

    @app.get("/file")
    async def download(request: Any) -> FileResponse:
        return FileResponse(served)

    async with TestClient(app) as client:
        response = await client.get("/file", headers={"accept-encoding": "gzip"})

    assert response.status == 200
    assert response.header("content-encoding") is None
    assert response.body == served.read_bytes()


@pytest.mark.asyncio
async def test_compress_streaming_off_leaves_a_streaming_response_alone() -> None:

    async def source():
        yield b"chunk" * 500

    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(compression=CompressionPolicy(minimum_size=0, compress_streaming=False))
    )

    @app.get("/")
    async def index(request: Any) -> StreamingResponse:
        return StreamingResponse(source(), headers=[(b"content-type", b"text/plain")])

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "gzip"})

    assert response.header("content-encoding") is None
    assert response.body == b"chunk" * 500
