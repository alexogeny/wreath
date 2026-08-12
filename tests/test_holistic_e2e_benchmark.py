"""The maximal instruction target remains a working declarative application."""

from __future__ import annotations

from compression import zstd
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks import holistic_e2e
from wreath.grpc import Unframer
from wreath.protobuf import decode as protobuf_decode
from wreath.testing import TestClient


class _Plan:
    result_names = ("id",)
    result_oids = (23,)


class _Connection:
    def __init__(self) -> None:
        self._plans: dict[str, _Plan] = {}

    async def fetch(self, query: str, *values: int) -> list[list[int]]:
        self._plans[query] = _Plan()
        assert all(type(value) is int for value in values)
        return [[42]]


class _Client:
    async def get(self, path: str) -> Any:
        assert path == "/data"
        return SimpleNamespace(status=200)


async def _dependencies() -> dict[str, Any]:
    return {"connection": _Connection(), "client": _Client()}


async def _request(client: TestClient, name: str, **extra_headers: str) -> Any:
    arm = holistic_e2e.ARMS[name]
    return await client.request(
        arm.method,
        arm.path,
        headers={**arm.headers, **extra_headers},
        content=arm.body,
    )


async def _drive_http2(app: Any, name: str) -> list[dict[str, Any]]:
    arm = holistic_e2e.ARMS[name]
    path, _, query = arm.path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": arm.http_version,
        "method": arm.method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "scheme": "https",
        "headers": [(key.lower().encode(), value.encode()) for key, value in arm.headers.items()],
        "client": ("127.0.0.1", 1234),
        "server": ("operations.example.com", 443),
    }
    incoming = [{"type": "http.request", "body": arm.body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if incoming:
            return incoming.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_holistic_target_reaches_template_egress(monkeypatch: Any) -> None:
    monkeypatch.setattr(holistic_e2e, "_e2e_ensure", _dependencies)
    async with TestClient(holistic_e2e.app) as client:
        response = await _request(client, "dashboard")

    assert response.status == 200
    headers = dict(response.headers)
    assert headers[b"content-type"] == b"text/html; charset=utf-8"
    assert headers[b"content-encoding"] == b"zstd"
    assert headers[b"cache-control"] == b"private, no-store"
    assert b"wreath_state=" in headers[b"set-cookie"]
    body = zstd.decompress(response.body)
    assert b"Quarterly &lt;report&gt;" in body
    assert b"holistic-user / 42" in body
    assert b"POST:/v1/holistic/42" in body
    assert b"gamma-3" in body
    assert b'data-buckets="730"' in body
    assert b'data-lines="288"' in body
    assert b'data-spines="' in body
    assert b'data-paths="11"' in body
    assert b'data-grid="' in body
    assert b'data-next="2026-' in body
    assert b'data-vector="128:128:30000/32"' in body
    assert b'data-page="12/48"' in body
    assert b'data-protobuf="' in body
    assert b'data-msgpack="' in body
    assert b'data-metrics="5"' in body
    assert b"<svg" in body and b'<path d="M' in body
    assert b'data-distance="732"' in body


@pytest.mark.asyncio
async def test_versioned_http_arms_cover_distinct_surfaces(monkeypatch: Any) -> None:
    monkeypatch.setattr(holistic_e2e, "_e2e_ensure", _dependencies)
    async with TestClient(holistic_e2e.app) as client:
        graphql = await _request(client, "graphql")
        crud = await _request(client, "crud-list")
        protobuf = await _request(client, "protobuf")
        messagepack = await _request(client, "messagepack")
        multipart = await _request(client, "multipart")
        sse = await _request(client, "sse")

    assert graphql.status == 200
    assert b'"account_id":42' in graphql.body
    assert b'"bucket_count":730' in graphql.body
    assert crud.status == 200
    assert crud.body == b'{"items":[{"id":42}],"page":1,"size":20}'
    assert protobuf.status == 200
    assert protobuf_decode(holistic_e2e.OperationsExport, protobuf.body).account_id == 42
    assert messagepack.status == 200
    assert dict(messagepack.headers)[b"content-type"] == b"application/msgpack"
    assert multipart.status == 200
    assert b'"filename":"fixes.csv"' in multipart.body
    assert b'"observations":730' in multipart.body
    assert sse.status == 200
    assert sse.body.count(b"event: report") == 4


@pytest.mark.asyncio
async def test_versioned_mcp_arms_share_an_authenticated_session(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(holistic_e2e, "_e2e_ensure", _dependencies)
    async with TestClient(holistic_e2e.app) as client:
        initialized = await _request(client, "mcp-initialize")
        session_id = dict(initialized.headers)[b"mcp-session-id"].decode()
        tool = await _request(client, "mcp-tool", **{"mcp-session-id": session_id})
        resource = await _request(client, "mcp-resource", **{"mcp-session-id": session_id})
        prompt = await _request(client, "mcp-prompt", **{"mcp-session-id": session_id})

    assert initialized.status == 200
    assert b'"protocolVersion":"2025-06-18"' in initialized.body
    assert b'"account_id":42' in tool.body
    assert b'"buckets":730' in tool.body
    assert b'\\"status\\":\\"nominal\\"' in resource.body
    assert b"Draft a concise handover" in prompt.body


@pytest.mark.asyncio
async def test_versioned_grpc_arm_runs_over_http2(monkeypatch: Any) -> None:
    monkeypatch.setattr(holistic_e2e, "_e2e_ensure", _dependencies)
    async with TestClient(holistic_e2e.app):
        sent = await _drive_http2(holistic_e2e.app, "grpc")

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 200
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    frames = Unframer().feed(body)
    assert len(frames) == 1
    export = protobuf_decode(holistic_e2e.OperationsExport, frames[0])
    assert export.account_id == 42
    assert export.bucket_count == 730


def test_every_holistic_arm_uses_the_versioned_router() -> None:
    assert holistic_e2e.ARMS
    assert all(arm.path.startswith("/v1/") for arm in holistic_e2e.ARMS.values())
