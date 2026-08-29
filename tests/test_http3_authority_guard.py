from pathlib import Path


def test_http3_refuses_duplicate_or_conflicting_routing_authorities() -> None:
    source = (Path(__file__).parents[1] / "src/wreath/_native/http3_asgi.c").read_text(
        encoding="utf-8"
    )
    start = source.index("start_request(WreathH3Stream *s)")
    end = source.index("end_headers_cb(", start)
    scope_builder = source[start:end]

    assert "PyObject *host = NULL" in scope_builder
    assert "if (host != NULL)" in scope_builder
    assert "h3_authorities_equal(authority, host, scheme)" in scope_builder
    assert "NGHTTP3_H3_MESSAGE_ERROR" in scope_builder
