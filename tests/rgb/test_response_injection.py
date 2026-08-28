"""Header/frame injection through response constructors (report 23: R-37..R-40)."""

from __future__ import annotations

import pytest

from wreath.response import (
    FileResponse,
    RedirectResponse,
    Response,
    ServerSentEvent,
    _encode_sse,
)


def _header(response, name: bytes) -> bytes:
    return next(value for key, value in response.headers if key.lower() == name)


class TestSetCookieAttributes:
    """R-37: control characters are rejected in name/value but not path/domain."""

    def test_control_character_in_path_is_refused(self):
        response = Response(b"")
        with pytest.raises(ValueError):
            response.set_cookie("sid", "abc", path="/\r\nSet-Cookie: admin=1")

    def test_control_character_in_domain_is_refused(self):
        response = Response(b"")
        with pytest.raises(ValueError):
            response.set_cookie("sid", "abc", domain="example.com\r\nX-Evil: 1")

    @pytest.mark.parametrize(
        "attributes",
        [
            {"path": "/; Secure"},
            {"domain": "example.com; Secure"},
        ],
    )
    def test_attribute_separator_in_path_or_domain_is_refused(self, attributes):
        response = Response(b"")
        with pytest.raises(ValueError, match="attribute separator"):
            response.set_cookie("sid", "abc", **attributes)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("sid; Domain=example.com", "abc"),
            ("sid", "abc; Domain=example.com"),
        ],
    )
    def test_attribute_separator_in_name_or_value_is_refused(self, name, value):
        with pytest.raises(ValueError, match="attribute separator"):
            Response(b"").set_cookie(name, value)

    def test_control_character_in_expires_is_refused(self):
        with pytest.raises(ValueError, match="cookie expires contains a control character"):
            Response(b"").set_cookie(
                "sid",
                "abc",
                expires="Thu, 01 Jan 1970 00:00:00 GMT\r\nX-Injected: yes",
            )

    def test_max_age_must_be_an_integer(self):
        with pytest.raises(TypeError, match="max_age must be an int"):
            Response(b"").set_cookie("sid", "abc", max_age="0; Secure")

    def test_ordinary_attributes_still_work(self):
        response = Response(b"")
        response.set_cookie("sid", "abc", path="/app", domain="example.com")
        assert _header(response, b"set-cookie") == (
            b"sid=abc; Path=/app; Domain=example.com; SameSite=Lax"
        )


class TestSSEFraming:
    """R-38: `event` and `id` are written raw, so a newline injects a frame."""

    def test_newline_in_event_name_is_refused(self):
        with pytest.raises(ValueError):
            _encode_sse(ServerSentEvent(data="ok", event="progress\n\ndata: injected"))

    def test_newline_in_id_is_refused(self):
        with pytest.raises(ValueError):
            _encode_sse(ServerSentEvent(data="ok", id="1\n\ndata: injected"))

    def test_multiline_data_is_still_framed_per_line(self):
        frame = _encode_sse(ServerSentEvent(data="a\nb", event="tick"))
        assert frame == b"event: tick\ndata: a\ndata: b\n\n"


class TestContentDisposition:
    """R-39: `filename` is interpolated inside quotes with no escaping."""

    def test_quote_in_filename_cannot_rewrite_the_header(self, tmp_path):
        target = tmp_path / "x.txt"
        target.write_text("hi")
        response = FileResponse(target, filename='a"; filename="b')
        value = _header(response, b"content-disposition")
        assert value.count(b'filename="') == 1

    def test_control_character_in_filename_is_refused(self, tmp_path):
        target = tmp_path / "x.txt"
        target.write_text("hi")
        with pytest.raises(ValueError):
            FileResponse(target, filename="a\r\nX-Evil: 1")


class TestRedirectScheme:
    """R-40: any scheme is accepted, `javascript:` included."""

    def test_javascript_scheme_is_refused(self):
        with pytest.raises(ValueError):
            RedirectResponse("javascript:alert(1)")

    def test_data_scheme_is_refused(self):
        with pytest.raises(ValueError):
            RedirectResponse("data:text/html,<script>alert(1)</script>")

    def test_ordinary_targets_are_unchanged(self):
        assert _header(RedirectResponse("/next?a=1"), b"location") == b"/next?a=1"
        assert (
            _header(RedirectResponse("https://example.com/x"), b"location")
            == b"https://example.com/x"
        )
        # A protocol-relative target stays allowed: it is same-scheme by design.
        assert _header(RedirectResponse("//example.com/x"), b"location") == b"//example.com/x"
