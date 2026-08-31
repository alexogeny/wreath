from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath._compression import _dcz_decompress, _RenderedFragments
from wreath.policy import compression as compression_module
from wreath.policy.compression import CompressionPolicy
from wreath.request import Request
from wreath.response import Response, StreamingResponse
from wreath.templates import Template


def _request(
    *,
    accepted: bytes = b"gzip",
    available: bytes | None = None,
    identity: object | None = None,
    method: str = "GET",
    scheme: str = "https",
) -> Request:
    headers = {b"accept-encoding": accepted, b"available-dictionary": available}
    return cast(
        Request,
        SimpleNamespace(
            identity=identity,
            method=method,
            scheme=scheme,
            _header_bytes=headers.get,
        ),
    )


def _fragment_policy(*, suffix: bytes = b"") -> tuple[CompressionPolicy, bytes, bytes]:
    prefix = b"<h1>dynamic</h1>"
    stable = b"<main>stable</main>"
    policy = CompressionPolicy(minimum_size=0)
    policy._configure_gzip_fragment(
        "html",
        prefix + stable + suffix,
        prefix_bytes=len(prefix),
        suffix_bytes=len(suffix),
    )
    return policy, prefix, stable


def test_fragment_body_refuses_an_unprepared_format() -> None:
    with pytest.raises(RuntimeError, match="is not prepared"):
        CompressionPolicy()._gzip_fragment_body("html", b"")


def test_fragment_body_refuses_a_stale_level() -> None:
    policy, prefix, _stable = _fragment_policy()
    policy.gzip_level += 1

    with pytest.raises(RuntimeError, match="no longer matches gzip_level"):
        policy._gzip_fragment_body("html", prefix)


def test_fragment_body_refuses_a_prefix_with_the_wrong_length() -> None:
    policy, _prefix, _stable = _fragment_policy()

    with pytest.raises(ValueError, match="exactly 16 bytes"):
        policy._gzip_fragment_body("html", b"short")


def test_fragment_body_refuses_a_prefix_bytes_subclass() -> None:
    policy, prefix, _stable = _fragment_policy()
    prefix_subclass = type("PrefixBytes", (bytes,), {})(prefix)

    with pytest.raises(ValueError, match="exactly 16 bytes"):
        policy._gzip_fragment_body("html", prefix_subclass)


def test_fragment_body_refuses_a_suffix_with_the_wrong_length() -> None:
    policy, prefix, _stable = _fragment_policy(suffix=b"footer")

    with pytest.raises(ValueError, match="exactly 6 bytes"):
        policy._gzip_fragment_body("html", prefix, b"short")


def test_fragment_body_refuses_a_suffix_bytes_subclass() -> None:
    policy, prefix, _stable = _fragment_policy(suffix=b"footer")
    suffix_subclass = type("SuffixBytes", (bytes,), {})(b"footer")

    with pytest.raises(ValueError, match="exactly 6 bytes"):
        policy._gzip_fragment_body("html", prefix, suffix_subclass)


def test_authenticated_fragment_render_does_not_prepare_dcz() -> None:
    policy, prefix, stable = _fragment_policy()
    token = policy._configure_dcz_dictionary("html", b"dictionary" + stable)
    template = Template.from_string("<h1>{{ title }}</h1>")

    body = policy._dcz_fragment_render(
        _request(accepted=b"dcz, gzip", available=token, identity=object()),
        "html",
        template,
        {"title": "dynamic"},
    )

    assert type(body) is bytes
    assert body == prefix + stable


def test_anonymous_fragment_render_prepares_dcz() -> None:
    policy, prefix, stable = _fragment_policy()
    token = policy._configure_dcz_dictionary("html", b"dictionary" + stable)
    template = Template.from_string("<h1>{{ title }}</h1>")

    body = policy._dcz_fragment_render(
        _request(accepted=b"dcz, gzip", available=token),
        "html",
        template,
        {"title": "dynamic"},
    )

    assert type(body) is _RenderedFragments
    assert body.materialize() == prefix + stable


@pytest.mark.asyncio
async def test_head_response_is_not_compressed() -> None:
    response = Response(b"compressible" * 20, media_type=b"text/plain")

    result = await CompressionPolicy(minimum_size=0).after(_request(method="HEAD"), response)

    assert result is response
    assert b"content-encoding" not in dict(response.headers)


@pytest.mark.asyncio
async def test_bodyless_status_is_not_compressed() -> None:
    response = Response(b"compressible" * 20, status=204, media_type=b"text/plain")

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert b"content-encoding" not in dict(response.headers)


@pytest.mark.asyncio
async def test_partial_response_is_not_compressed() -> None:
    response = Response(b"compressible" * 20, status=206, media_type=b"text/plain")

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert b"content-encoding" not in dict(response.headers)


@pytest.mark.asyncio
async def test_existing_content_encoding_prevents_compression() -> None:
    response = Response(
        b"compressible" * 20,
        headers=[(b"content-encoding", b"identity")],
        media_type=b"text/plain",
    )

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert response.body == b"compressible" * 20
    assert response.headers.count((b"content-encoding", b"identity")) == 1


@pytest.mark.asyncio
async def test_existing_content_range_prevents_compression() -> None:
    response = Response(
        b"compressible" * 20,
        headers=[(b"content-range", b"bytes 0-9/100")],
        media_type=b"text/plain",
    )

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert response.body == b"compressible" * 20
    assert b"content-encoding" not in dict(response.headers)


@pytest.mark.asyncio
async def test_missing_content_type_prevents_compression() -> None:
    response = Response(b"compressible" * 20, media_type=b"")

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert b"content-encoding" not in dict(response.headers)
    assert response.body == b"compressible" * 20


@pytest.mark.asyncio
async def test_incompressible_content_type_prevents_compression() -> None:
    response = Response(b"compressible" * 20, media_type=b"image/png")

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert b"content-encoding" not in dict(response.headers)
    assert response.body == b"compressible" * 20


@pytest.mark.asyncio
async def test_dcz_is_not_available_over_plain_http() -> None:
    body = b'{"stable":true}' * 100
    policy = CompressionPolicy(minimum_size=0)
    token = policy._configure_dcz_dictionary("application/json", body)
    response = Response(body, media_type=b"application/json")

    await policy.after(_request(accepted=b"dcz, gzip", available=token, scheme="http"), response)

    assert dict(response.headers)[b"content-encoding"] == b"gzip"
    assert b"available-dictionary" not in dict(response.headers)[b"vary"]


@pytest.mark.asyncio
async def test_dcz_is_not_available_for_a_streaming_response() -> None:
    async def body():
        yield b'{"stable":true}' * 100

    dictionary = b'{"stable":true}' * 100
    policy = CompressionPolicy(minimum_size=0)
    token = policy._configure_dcz_dictionary("application/json", dictionary)
    stream = body()
    response = StreamingResponse(stream, headers=[(b"content-type", b"application/json")])

    await policy.after(_request(accepted=b"dcz, gzip", available=token), response)

    assert dict(response.headers)[b"content-encoding"] == b"gzip"
    await stream.aclose()


@pytest.mark.asyncio
async def test_malformed_etag_prevents_compression() -> None:
    body = b"compressible" * 20
    response = Response(body, headers=[(b"etag", b"unquoted")], media_type=b"text/plain")

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    assert response.body == body
    assert b"content-encoding" not in dict(response.headers)


@pytest.mark.asyncio
async def test_zstd_uses_its_configured_level(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[bytes, int]] = []
    monkeypatch.setattr(
        compression_module,
        "zstd_compress",
        lambda body, level: observed.append((body, level)) or b"encoded",
    )
    policy = CompressionPolicy(minimum_size=0, gzip_level=1, zstd_level=9)
    response = Response(b"compressible", media_type=b"text/plain")

    await policy.after(_request(accepted=b"zstd"), response)

    assert observed == [(b"compressible", 9)]


@pytest.mark.asyncio
async def test_gzip_uses_its_configured_level(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[int] = []
    monkeypatch.setattr(
        compression_module,
        "_gzip_fragment_compress_with",
        lambda workspace, body, level, content_type, fragments: (
            observed.append(level) or b"encoded"
        ),
    )
    policy = CompressionPolicy(minimum_size=0, gzip_level=1, zstd_level=9)
    response = Response(b"compressible", media_type=b"text/plain")

    await policy.after(_request(), response)

    assert observed == [1]


@pytest.mark.asyncio
async def test_response_below_minimum_size_is_not_compressed() -> None:
    response = Response(b"short", media_type=b"text/plain")

    await CompressionPolicy(minimum_size=6).after(_request(), response)

    assert response.body == b"short"
    assert b"content-encoding" not in dict(response.headers)


@pytest.mark.asyncio
async def test_exact_prepared_body_uses_fragment_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, prefix, stable = _fragment_policy()
    body = policy._gzip_fragment_body("html", prefix)
    observed: list[Any] = []
    monkeypatch.setattr(
        compression_module,
        "_gzip_fragment_compress_with",
        lambda workspace, parts, level, content_type, fragments: (
            observed.append(parts) or b"encoded"
        ),
    )
    response = Response(body, media_type=b"text/html")

    await policy.after(_request(), response)

    assert len(observed) == 1
    fragment_parts = observed[0]
    assert isinstance(fragment_parts, tuple)
    assert fragment_parts[:2] == (prefix, b"")
    assert fragment_parts[2] in policy._gzip_fragments
    assert body == prefix + stable


@pytest.mark.asyncio
async def test_zstd_materializes_rendered_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    fragments = _RenderedFragments(b"prefix", b"stable")
    observed: list[bytes] = []
    monkeypatch.setattr(
        compression_module,
        "zstd_compress",
        lambda body, level: observed.append(body) or b"encoded",
    )
    response = Response(fragments, media_type=b"text/plain")

    await CompressionPolicy(minimum_size=0).after(_request(accepted=b"zstd"), response)

    assert type(observed[0]) is bytes
    assert observed == [b"prefixstable"]


@pytest.mark.asyncio
async def test_only_dcz_varies_on_the_available_dictionary() -> None:
    body = b'{"stable":true}' * 100
    policy = CompressionPolicy(minimum_size=0)
    token = policy._configure_dcz_dictionary("application/json", body)
    dcz_response = Response(body, media_type=b"application/json")
    gzip_response = Response(body, media_type=b"application/json")

    await policy.after(_request(accepted=b"dcz, gzip", available=token), dcz_response)
    await policy.after(_request(accepted=b"gzip", available=token), gzip_response)

    assert dict(dcz_response.headers)[b"content-encoding"] == b"dcz"
    assert _dcz_decompress(dcz_response.body, body, max_output_bytes=len(body)) == body
    assert b"available-dictionary" in dict(dcz_response.headers)[b"vary"]
    assert dict(gzip_response.headers)[b"content-encoding"] == b"gzip"
    assert b"available-dictionary" not in dict(gzip_response.headers)[b"vary"]


@pytest.mark.parametrize("etag", [None, b'"version"'])
@pytest.mark.asyncio
async def test_compression_updates_an_etag_only_when_present(etag: bytes | None) -> None:
    headers = [] if etag is None else [(b"etag", etag)]
    response = Response(b"compressible" * 20, headers=headers, media_type=b"text/plain")

    await CompressionPolicy(minimum_size=0).after(_request(), response)

    expected = None if etag is None else b'"version--gzip"'
    assert dict(response.headers).get(b"etag") == expected
