"""AWS Lambda Web Adapter for API Gateway payloads and Function URLs.

One adapter instance owns one event loop and one ASGI lifespan across warm
invocations. No server socket, thread, global loop, boto3, or vendor runtime
dependency is involved.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlencode

from ._asgi_driver import ASGI, WarmASGIDriver, _encode_http_header

__all__ = ["LambdaAdapter", "LambdaEventError"]


class LambdaEventError(ValueError):
    """An event is not an API Gateway v1/v2 or Lambda Function URL request."""


class LambdaAdapter:
    """Turn API Gateway REST/HTTP API and Function URL events into ASGI.

    Construct this object at module scope as the Lambda handler. Startup runs on
    the first invocation and remains live across warm invocations. Call
    `close` in local hosts/tests; AWS may freeze or terminate an execution
    environment without offering a shutdown callback.
    """

    __slots__ = ("_driver",)

    def __init__(self, app: Any) -> None:
        self._driver = WarmASGIDriver(app, owner="LambdaAdapter")

    def __call__(self, event: Mapping[str, Any], context: Any) -> dict[str, Any]:
        version, scope, body = _scope(event, context)
        response = self._driver.invoke(scope, body)
        return _response(version, response.status, response.headers, response.body)

    def close(self) -> None:
        """Run ASGI shutdown and close the adapter-owned loop exactly once."""
        self._driver.close()

    def __enter__(self) -> LambdaAdapter:
        return self

    def __exit__(self, *_error: Any) -> None:
        self.close()


def _scope(
    event: Mapping[str, Any], context: Any
) -> tuple[str, dict[str, Any], bytes]:
    version = str(event.get("version", "1.0"))
    if version not in {"1.0", "2.0"}:
        raise LambdaEventError(
            f"unsupported Lambda payload version {version!r}; expected '1.0' or '2.0'"
        )
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        raise LambdaEventError("Lambda HTTP event needs a requestContext object")
    if version == "2.0":
        http = request_context.get("http")
        if not isinstance(http, Mapping):
            raise LambdaEventError("payload v2 requestContext needs an http object")
        method = _text(http, "method")
        raw_path_text = event.get("rawPath", "/")
        if not isinstance(raw_path_text, str):
            raise LambdaEventError("payload v2 rawPath must be a string")
        try:
            raw_path = raw_path_text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise LambdaEventError(
                "payload v2 rawPath must be ASCII with non-ASCII bytes percent-encoded"
            ) from exc
        decoded_path = http.get("path")
        if decoded_path is None:
            try:
                path = unquote(raw_path_text, encoding="utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise LambdaEventError(
                    "payload v2 rawPath is not valid percent-encoded UTF-8"
                ) from exc
        elif isinstance(decoded_path, str):
            path = decoded_path
        else:
            raise LambdaEventError(
                "payload v2 requestContext http path must be a string"
            )
        query = str(event.get("rawQueryString", ""))
        source_ip = str(http.get("sourceIp", ""))
        header_pairs = _v2_headers(event)
    else:
        method = _text(event, "httpMethod")
        path = str(event.get("path", "/"))
        raw_path = path.encode("utf-8")
        query = _v1_query(event)
        identity = request_context.get("identity", {})
        source_ip = str(identity.get("sourceIp", "")) if isinstance(identity, Mapping) else ""
        header_pairs = _v1_headers(event)
    raw_body = event.get("body")
    if raw_body is None:
        body = b""
    elif not isinstance(raw_body, str):
        raise LambdaEventError("Lambda HTTP event body must be a string or null")
    elif event.get("isBase64Encoded") is True:
        try:
            body = base64.b64decode(raw_body, validate=True)
        except ValueError as exc:
            raise LambdaEventError("Lambda HTTP event body is not valid base64") from exc
    else:
        body = raw_body.encode("utf-8")
    header_map = {name: value for name, value in header_pairs}
    host = header_map.get(b"host", b"lambda").decode("latin-1")
    scheme = header_map.get(b"x-forwarded-proto", b"https").decode("ascii", "strict")
    scope = {
        "type": "http",
        "asgi": ASGI,
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": scheme,
        "path": path,
        "raw_path": raw_path,
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": list(header_pairs),
        "client": (source_ip, None) if source_ip else None,
        "server": (host, None),
        "extensions": (
            {"wreath.google": event["_wreath_google"]}
            if "_wreath_google" in event
            else {"wreath.lambda": {"event": event, "context": context}}
        ),
    }
    return version, scope, body


def _text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LambdaEventError(f"Lambda HTTP event needs string {key!r}")
    return value


def _header(name: Any, value: Any) -> tuple[bytes, bytes]:
    return _encode_http_header(
        name, value, owner="Lambda", error_type=LambdaEventError
    )


def _v2_headers(event: Mapping[str, Any]) -> tuple[tuple[bytes, bytes], ...]:
    raw = event.get("headers", {})
    if not isinstance(raw, Mapping):
        raise LambdaEventError("payload v2 headers must be an object")
    headers = [_header(name, value) for name, value in raw.items()]
    cookies = event.get("cookies", [])
    if not isinstance(cookies, list) or any(not isinstance(item, str) for item in cookies):
        raise LambdaEventError("payload v2 cookies must be a list of strings")
    if cookies:
        headers.append((b"cookie", b"; ".join(item.encode("latin-1") for item in cookies)))
    return tuple(headers)


def _v1_headers(event: Mapping[str, Any]) -> tuple[tuple[bytes, bytes], ...]:
    multiple = event.get("multiValueHeaders")
    if isinstance(multiple, Mapping):
        return tuple(
            _header(name, value)
            for name, values in multiple.items()
            for value in (values if isinstance(values, list) else [values])
        )
    raw = event.get("headers", {})
    if not isinstance(raw, Mapping):
        raise LambdaEventError("payload v1 headers must be an object")
    return tuple(_header(name, value) for name, value in raw.items())


def _v1_query(event: Mapping[str, Any]) -> str:
    multiple = event.get("multiValueQueryStringParameters")
    if isinstance(multiple, Mapping):
        return urlencode(
            [(str(name), str(value)) for name, values in multiple.items()
             for value in (values if isinstance(values, list) else [values])]
        )
    query = event.get("queryStringParameters")
    return urlencode(query) if isinstance(query, Mapping) else ""


def _response(
    version: str, status: int, headers: tuple[tuple[bytes, bytes], ...], body: bytes
) -> dict[str, Any]:
    decoded = [(name.decode("latin-1"), value.decode("latin-1")) for name, value in headers]
    content_type = next(
        (value for name, value in decoded if name.lower() == "content-type"), ""
    )
    textual = content_type.startswith("text/") or any(
        marker in content_type for marker in ("json", "xml", "javascript", "form-urlencoded")
    )
    encoded = False
    if textual:
        try:
            rendered = body.decode("utf-8")
        except UnicodeDecodeError:
            rendered = base64.b64encode(body).decode("ascii")
            encoded = True
    else:
        rendered = base64.b64encode(body).decode("ascii")
        encoded = True
    result: dict[str, Any] = {
        "statusCode": status,
        "body": rendered,
        "isBase64Encoded": encoded,
    }
    if version == "2.0":
        cookie_values = [value for name, value in decoded if name.lower() == "set-cookie"]
        ordinary: dict[str, str] = {}
        ordinary_names: dict[str, str] = {}
        for name, value in decoded:
            normalized = name.lower()
            if normalized != "set-cookie":
                ordinary_names.setdefault(normalized, name)
                ordinary[normalized] = (
                    value
                    if normalized not in ordinary
                    else f"{ordinary[normalized]},{value}"
                )
        result["headers"] = {
            ordinary_names[name]: value for name, value in ordinary.items()
        }
        if cookie_values:
            result["cookies"] = cookie_values
    else:
        multiple: dict[str, list[str]] = {}
        multiple_names: dict[str, str] = {}
        for name, value in decoded:
            normalized = name.lower()
            multiple_names.setdefault(normalized, name)
            multiple.setdefault(normalized, []).append(value)
        result["multiValueHeaders"] = {
            multiple_names[name]: values for name, values in multiple.items()
        }
    return result
