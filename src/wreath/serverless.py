"""Serverless deployment adapters without an AWS-only application shape."""

from __future__ import annotations

import importlib
from typing import Any

from ._asgi_driver import ASGI, WarmASGIDriver, _encode_http_header
from ._headers import find_header

__all__ = [
    "GoogleFunctionAdapter",
    "azure_function_app",
]


class GoogleFunctionAdapter:
    """Adapt a Google Functions Framework HTTP request to one warm ASGI app.

    The request is consumed structurally, so importing Wreath does not require
    Flask or Functions Framework. The returned `(body, status, headers)` is
    the response form that framework accepts. One underlying adapter owns the
    event loop and ASGI lifespan across warm calls.
    """

    __slots__ = ("_driver",)

    def __init__(self, app: Any) -> None:
        self._driver = WarmASGIDriver(app, owner="GoogleFunctionAdapter")

    def __call__(self, request: Any) -> tuple[bytes, int, list[tuple[str, str]]]:
        method = getattr(request, "method", None)
        path = getattr(request, "path", None)
        if not isinstance(method, str) or not method:
            raise TypeError("Google HTTP request needs a non-empty string method")
        if not isinstance(path, str) or not path.startswith("/"):
            raise TypeError("Google HTTP request needs an absolute path")
        query = getattr(request, "query_string", b"")
        if isinstance(query, bytes):
            query_string = query
        elif isinstance(query, str):
            query_string = query.encode("ascii")
        else:
            raise TypeError("Google HTTP request query_string must be bytes or str")
        raw_headers = getattr(request, "headers", None)
        if raw_headers is None or not callable(getattr(raw_headers, "items", None)):
            raise TypeError("Google HTTP request needs mapping-like headers")
        headers = [_google_header(name, value) for name, value in raw_headers.items()]
        get_data = getattr(request, "get_data", None)
        if not callable(get_data):
            raise TypeError("Google HTTP request needs get_data() returning bytes")
        body = get_data()
        if not isinstance(body, bytes):
            raise TypeError("Google HTTP request get_data() must return bytes")
        host = getattr(request, "host", None)
        if isinstance(host, str) and host and find_header(headers, b"host") is None:
            headers.append(_google_header("host", host))
        scheme = getattr(request, "scheme", None)
        if not isinstance(scheme, str) or not scheme:
            scheme = "https"
        remote = str(getattr(request, "remote_addr", "") or "")
        scope = {
            "type": "http",
            "asgi": ASGI,
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query_string,
            "root_path": "",
            "headers": headers,
            "client": (remote, None) if remote else None,
            "server": (host, None) if isinstance(host, str) and host else None,
            "extensions": {"wreath.google": {"request": request}},
        }
        response = self._driver.invoke(scope, body)
        response_headers = [
            (name.decode("latin-1"), value.decode("latin-1")) for name, value in response.headers
        ]
        return response.body, response.status, response_headers

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> GoogleFunctionAdapter:
        return self

    def __exit__(self, *_error: Any) -> None:
        self.close()


def azure_function_app(
    app: Any, *, http_auth_level: Any = "FUNCTION", function_name: str = "http_app_func"
) -> Any:
    """Return Azure Functions' native ASGI host around `app`.

    Azure already speaks ASGI through `AsgiFunctionApp`; this helper keeps the
    platform import optional and refuses at configuration time when its SDK is
    absent instead of maintaining a second request translator.
    """
    try:
        adapter = importlib.import_module("azure.functions").AsgiFunctionApp
    except (AttributeError, ImportError) as exc:
        raise RuntimeError(
            "azure_function_app requires the optional 'azure-functions' package; "
            "install azure-functions in the deployment environment"
        ) from exc
    return adapter(app=app, http_auth_level=http_auth_level, function_name=function_name)


def _google_header(name: Any, value: Any) -> tuple[bytes, bytes]:
    return _encode_http_header(name, value, owner="Google HTTP", error_type=TypeError)
