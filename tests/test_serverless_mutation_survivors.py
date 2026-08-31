from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wreath.serverless import GoogleFunctionAdapter


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], bytes]] = []

    def invoke(self, scope: dict[str, Any], body: bytes) -> SimpleNamespace:
        self.calls.append((scope, body))
        return SimpleNamespace(body=b"response", status=201, headers=[(b"x-id", b"one")])


def _adapter() -> tuple[GoogleFunctionAdapter, _Driver]:
    adapter = object.__new__(GoogleFunctionAdapter)
    driver = _Driver()
    adapter._driver = driver
    return adapter, driver


def _request(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "method": "post",
        "path": "/items",
        "query_string": b"page=1",
        "headers": {"accept": "application/json"},
        "get_data": lambda: b"request",
        "host": "api.example.test",
        "scheme": "http",
        "remote_addr": "192.0.2.1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("method", [None, "", b"GET"])
def test_google_adapter_refuses_every_non_string_or_empty_method(method: object) -> None:
    adapter, _driver = _adapter()

    with pytest.raises(TypeError, match="non-empty string method"):
        adapter(_request(method=method))


@pytest.mark.parametrize("path", [None, "relative", b"/bytes"])
def test_google_adapter_refuses_every_non_string_or_relative_path(path: object) -> None:
    adapter, _driver = _adapter()

    with pytest.raises(TypeError, match="absolute path"):
        adapter(_request(path=path))


def test_google_adapter_requires_mapping_like_headers() -> None:
    adapter, _driver = _adapter()

    with pytest.raises(TypeError, match="mapping-like headers"):
        adapter(_request(headers=None))
    with pytest.raises(TypeError, match="mapping-like headers"):
        adapter(_request(headers=SimpleNamespace(items=1)))


def test_google_adapter_requires_a_callable_byte_body_reader() -> None:
    adapter, _driver = _adapter()

    with pytest.raises(TypeError, match="needs get_data"):
        adapter(_request(get_data=None))
    with pytest.raises(TypeError, match="must return bytes"):
        adapter(_request(get_data=lambda: "text"))


def test_google_adapter_projects_every_present_request_field() -> None:
    adapter, driver = _adapter()

    result = adapter(_request(query_string="page=2"))
    scope, body = driver.calls[0]

    assert result == (b"response", 201, [("x-id", "one")])
    assert body == b"request"
    assert scope["method"] == "POST"
    assert scope["scheme"] == "http"
    assert scope["path"] == "/items"
    assert scope["raw_path"] == b"/items"
    assert scope["query_string"] == b"page=2"
    assert scope["client"] == ("192.0.2.1", None)
    assert scope["server"] == ("api.example.test", None)
    assert (b"host", b"api.example.test") in scope["headers"]
    assert scope["extensions"]["wreath.google"]["request"].method == "post"


@pytest.mark.parametrize("scheme", [None, "", 7])
def test_google_adapter_defaults_each_missing_or_invalid_scheme(scheme: object) -> None:
    adapter, driver = _adapter()

    adapter(_request(scheme=scheme))

    assert driver.calls[0][0]["scheme"] == "https"


@pytest.mark.parametrize("remote", [None, "", 0])
def test_google_adapter_omits_each_empty_remote_address(remote: object) -> None:
    adapter, driver = _adapter()

    adapter(_request(remote_addr=remote))

    assert driver.calls[0][0]["client"] is None


@pytest.mark.parametrize("host", [None, "", 7])
def test_google_adapter_omits_each_empty_or_non_string_server(host: object) -> None:
    adapter, driver = _adapter()

    adapter(_request(host=host))
    scope = driver.calls[0][0]

    assert scope["server"] is None
    assert all(name != b"host" for name, _value in scope["headers"])


def test_google_adapter_preserves_an_explicit_host_header() -> None:
    adapter, driver = _adapter()

    adapter(_request(headers={"host": "forwarded.example"}))

    assert driver.calls[0][0]["headers"] == [(b"host", b"forwarded.example")]


def test_google_adapter_refuses_a_non_text_query_string() -> None:
    adapter, _driver = _adapter()

    with pytest.raises(TypeError, match="query_string must be bytes or str"):
        adapter(_request(query_string=7))
