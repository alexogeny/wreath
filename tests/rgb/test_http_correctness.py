from __future__ import annotations

from wreath import Wreath
from wreath.testing import TestClient


def _header(headers, name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


class TestBodylessStatuses:
    """G-42: a 204/304 built with a body still emits it, which is a framing
    error (RFC 9110 §6.4.1 -- neither may carry content)."""

    async def test_a_204_sends_no_body(self):
        from wreath.response import Response

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await Response(b"leftover", status=204)(send)
        assert b"".join(m.get("body", b"") for m in sent) == b""

    async def test_a_304_sends_no_body(self):
        from wreath.response import Response

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await Response(b"leftover", status=304)(send)
        assert b"".join(m.get("body", b"") for m in sent) == b""

    async def test_an_ordinary_response_is_unaffected(self):
        from wreath.response import Response

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await Response(b"hello")(send)
        assert b"".join(m.get("body", b"") for m in sent) == b"hello"


class TestAcceptLanguageQuality:
    """G-45: `q` is read only from the first `;`-parameter, so
    `;charset=utf-8;q=0.1` scores 1.0 and wins."""

    def _locale(self, header: str) -> str:
        from wreath.request import Request

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"accept-language", header.encode("latin-1"))],
            },
            None,
        )
        return request.locale

    def test_a_trailing_q_is_read(self):
        assert self._locale("de;charset=utf-8;q=0.1, en;q=0.9") == "en"

    def test_a_leading_q_still_works(self):
        assert self._locale("de;q=0.1, en;q=0.9") == "en"

    def test_order_breaks_a_tie(self):
        assert self._locale("fr, de") == "fr"

    def test_a_malformed_header_falls_back(self):
        assert self._locale(";;;") == "en"

    def test_a_refused_tag_is_not_returned(self):
        assert self._locale("de;q=0") == "en"

    def test_a_refused_tag_loses_to_an_accepted_one(self):
        assert self._locale("de;q=0, fr;q=0.5") == "fr"

    def test_every_tag_refused_falls_back(self):
        assert self._locale("de;q=0, fr;q=0") == "en"


class TestCorsPreflightValidation:
    """G-61: the preflight echoes the configured allow-list without checking
    what was actually requested. G-62: origin matching is exact and
    case-sensitive with no normalization."""

    def _middleware(self, **kwargs):
        from wreath.policy.cors import CorsPolicy

        return CorsPolicy(
            allow_origins=["https://app.example"],
            allow_methods=("GET", "POST"),
            **kwargs,
        )

    class _Preflight:
        method = "OPTIONS"

        def __init__(self, origin, requested="POST", headers=None):
            self._origin = origin
            self._requested = requested
            self._headers = headers

        def header(self, name, default=None):
            if name == "origin":
                return self._origin
            if name == "access-control-request-method":
                return self._requested
            if name == "access-control-request-headers":
                return self._headers
            return default

    async def test_a_method_outside_the_allow_list_is_refused(self):
        middleware = self._middleware()
        response = await middleware._ingress(
            self._Preflight("https://app.example", requested="DELETE")
        )
        assert response is not None and response.status == 403

    async def test_an_allowed_method_still_passes(self):
        middleware = self._middleware()
        response = await middleware._ingress(
            self._Preflight("https://app.example", requested="POST")
        )
        assert response is not None and response.status == 204

    async def test_the_origin_scheme_and_host_are_compared_case_insensitively(self):
        middleware = self._middleware()
        response = await middleware._ingress(self._Preflight("HTTPS://App.Example"))
        assert response is not None and response.status == 204


class TestRateLimitHeaders:
    """G-63: an allowed request carries no rate-limit headers at all, so a
    client can only discover the policy by hitting it.

    The policy rides the *refusal* rather than every response: advertising it
    globally costs a global `after` hook, which `wreath-request-trace` priced at
    +18 crossings per request. The remaining allowance stays absent even there
    for an allowed request -- neither store can report it, and a guess is worse
    than nothing. See report 23 G-63."""

    async def test_an_allowed_request_is_not_made_more_expensive(self):
        from wreath.policy.ratelimit import RateLimitPolicy

        middleware = RateLimitPolicy(limit=5, window=60.0)
        assert not hasattr(middleware, "after"), (
            "a global after hook prices every successful request"
        )

    async def test_a_refused_request_still_carries_retry_after(self):
        from wreath.policy.ratelimit import RateLimitPolicy

        middleware = RateLimitPolicy(limit=1, window=60.0)

        class _Request:
            method = "GET"
            path = "/x"
            client = ("198.51.100.5", 5000)
            identity = None

        middleware._ingress_sync(_Request())
        refused = middleware._ingress_sync(_Request())
        assert refused.status == 429
        assert _header(refused.headers, b"retry-after") is not None
        assert _header(refused.headers, b"x-ratelimit-remaining") == b"0"


class TestConditionalRequests:
    """G-30: `If-None-Match` is compared with `==`, so a client sending a list
    (RFC 9110 §13.1.2 allows one) or a proxy-rewritten tag revalidates the whole
    body every time. G-85: no `Last-Modified`."""

    async def test_a_list_of_etags_still_matches(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        app = Wreath()
        app.static("/assets", str(tmp_path))

        async with TestClient(app) as client:
            first = await client.get("/assets/a.txt")
            etag = first.header("etag")
            second = await client.get(
                "/assets/a.txt",
                headers={"if-none-match": f'W/"other", {etag}'},
            )
        assert second.status == 304

    async def test_a_star_matches_anything(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        app = Wreath()
        app.static("/assets", str(tmp_path))

        async with TestClient(app) as client:
            response = await client.get("/assets/a.txt", headers={"if-none-match": "*"})
        assert response.status == 304

    async def test_a_static_file_carries_last_modified(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        app = Wreath()
        app.static("/assets", str(tmp_path))

        async with TestClient(app) as client:
            response = await client.get("/assets/a.txt")
        assert response.header("last-modified") is not None

    async def test_an_unrelated_etag_still_transfers(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        app = Wreath()
        app.static("/assets", str(tmp_path))

        async with TestClient(app) as client:
            response = await client.get("/assets/a.txt", headers={"if-none-match": '"nope"'})
        assert response.status == 200


class TestStaticDirectoryRedirect:
    """G-86: `/dir` serves `index.html` without redirecting to `/dir/`, so every
    relative link in that page resolves one level up."""

    async def test_a_directory_without_a_slash_redirects(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "index.html").write_text("<p>hi</p>")
        app = Wreath()
        app.static("/assets", str(tmp_path))

        async with TestClient(app) as client:
            response = await client.get("/assets/sub")
        assert response.status in (301, 308)
        assert response.header("location") == "/assets/sub/"

    async def test_a_directory_with_a_slash_serves_the_index(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "index.html").write_text("<p>hi</p>")
        app = Wreath()
        app.static("/assets", str(tmp_path))

        async with TestClient(app) as client:
            response = await client.get("/assets/sub/")
        assert response.status == 200
        assert b"hi" in response.body
