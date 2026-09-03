from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.testing import TestClient


@pytest.mark.asyncio
async def test_api_catalog_discovers_the_application_specification_and_docs() -> None:
    app = Wreath()

    @app.get("/")
    async def root(request: Any) -> str:
        return "api"

    app.enable_api_docs(environments=None)
    app.enable_api_catalog()

    async with TestClient(app, headers={"host": "api.example"}) as client:
        response = await client.get("/.well-known/api-catalog")

    assert response.status == 200
    assert dict(response.headers)[b"content-type"] == (
        b'application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"'
    )
    assert response.json() == {
        "linkset": [
            {
                "anchor": "http://api.example/",
                "service-desc": [
                    {
                        "href": "http://api.example/openapi.json",
                        "type": "application/json",
                    }
                ],
                "service-doc": [
                    {
                        "href": "http://api.example/docs",
                        "type": "text/html",
                    }
                ],
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("host", "victim.example"), ("host", "attacker.example")],
        [("host", "attacker.example/path")],
    ],
)
async def test_api_catalog_does_not_publish_an_ambiguous_authority(
    headers: list[tuple[str, str]],
) -> None:
    app = Wreath()
    app.enable_api_catalog(api_path="/", spec_path="/openapi.json", docs_path=None)

    async with TestClient(app) as client:
        response = await client.get("/.well-known/api-catalog", headers=headers)

    assert response.json() == {
        "linkset": [
            {
                "anchor": "/",
                "service-desc": [{"href": "/openapi.json", "type": "application/json"}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_api_catalog_head_advertises_the_catalog_relation() -> None:
    app = Wreath()
    app.enable_api_catalog(api_path="/v2", spec_path=None, docs_path=None)

    async with TestClient(app, headers={"host": "api.example"}) as client:
        response = await client.head("/.well-known/api-catalog")

    assert response.status == 200
    assert response.body == b""
    assert dict(response.headers)[b"link"] == (
        b'</.well-known/api-catalog>; rel="api-catalog"; type="application/linkset+json"'
    )


def test_api_catalog_refuses_a_relative_api_path_without_a_leading_slash() -> None:
    app = Wreath()

    with pytest.raises(ValueError, match="api_path.*begin with '/'"):
        app.enable_api_catalog(api_path="v2")
