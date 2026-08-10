"""A tool's `inputSchema` and the same handler's OpenAPI schema must agree.

Their agreeing is what justifies reusing the binding layer instead of writing a
second one. The two renderings differ in exactly one respect -- where
a dataclass definition lives, `#/$defs/...` for a standalone JSON Schema against
`#/components/schemas/...` for an OpenAPI document -- and every assertion here
pins that difference so a future change to either renderer breaks loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

import pytest

from wreath import Wreath
from wreath.binding import Body, Cookie, File, Header, Query
from wreath.mcp import MCP, ToolSignatureError
from wreath.openapi import generate_openapi


@dataclass
class SightingQuery:
    species: str
    since: str | None = None
    tags: list[str] | None = None


async def find_sightings(
    request,
    query: Annotated[SightingQuery, Body()],
    limit: int = 20,
    order: Literal["asc", "desc"] = "asc",
) -> dict:
    """Find recent sightings of a species."""
    return {}


def tool_schema(handler, name: str = "find_sightings") -> dict:
    mcp = MCP(name="camera-trap", version="1.0.0")
    mcp.tool(handler, name=name)
    return mcp.tools[0].input_schema


def openapi_operation(handler) -> tuple[dict, dict]:
    app = Wreath()
    app.post("/sightings")(handler)
    document = generate_openapi(app)
    return document, document["paths"]["/sightings"]["post"]


def test_body_and_scalars_become_one_arguments_object() -> None:
    schema = tool_schema(find_sightings)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"query", "limit", "order"}
    assert schema["required"] == ["query"]
    assert schema["properties"]["limit"] == {"type": "integer", "default": 20}
    assert schema["properties"]["order"] == {"enum": ["asc", "desc"], "default": "asc"}


def test_a_dataclass_argument_is_a_ref_into_defs() -> None:
    schema = tool_schema(find_sightings)
    assert schema["properties"]["query"] == {"$ref": "#/$defs/SightingQuery"}
    assert set(schema["$defs"]) == {"SightingQuery"}


def test_the_definition_matches_the_openapi_component_byte_for_byte() -> None:
    schema = tool_schema(find_sightings)
    document, _operation = openapi_operation(find_sightings)
    assert schema["$defs"]["SightingQuery"] == document["components"]["schemas"]["SightingQuery"]


def test_scalar_properties_match_the_openapi_parameters() -> None:
    schema = tool_schema(find_sightings)
    _document, operation = openapi_operation(find_sightings)
    by_name = {parameter["name"]: parameter for parameter in operation["parameters"]}
    for name in ("limit", "order"):
        assert schema["properties"][name] == by_name[name]["schema"]
    assert by_name["limit"]["required"] is False


def test_the_body_property_matches_the_openapi_request_body_modulo_the_ref_base() -> None:
    schema = tool_schema(find_sightings)
    _document, operation = openapi_operation(find_sightings)
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/SightingQuery"}
    assert schema["properties"]["query"] == {
        "$ref": body["$ref"].replace("#/components/schemas/", "#/$defs/")
    }


def test_a_request_only_tool_takes_no_arguments() -> None:
    async def heartbeat(request) -> dict:
        """Report that the server is alive."""
        return {}

    schema = tool_schema(heartbeat, name="heartbeat")
    assert schema == {"type": "object", "properties": {}, "additionalProperties": False}


def test_an_optional_scalar_is_a_union_with_null() -> None:
    async def search(request, needle: str | None = None) -> dict:
        """Search."""
        return {}

    schema = tool_schema(search, name="search")
    assert schema["properties"]["needle"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    assert "required" not in schema


def test_a_query_alias_is_the_argument_name() -> None:
    async def search(request, needle: Annotated[str, Query(alias="q")]) -> dict:
        """Search."""
        return {}

    schema = tool_schema(search, name="search")
    assert schema["required"] == ["q"]


def test_a_header_parameter_is_refused_at_registration() -> None:
    async def probe(request, token: Annotated[str, Header()]) -> dict:
        """Probe."""
        return {}

    with pytest.raises(ToolSignatureError) as caught:
        tool_schema(probe, name="probe")
    assert "a header" in str(caught.value)
    assert "token" in str(caught.value)


def test_a_cookie_parameter_is_refused_at_registration() -> None:
    async def probe(request, session: Annotated[str, Cookie()]) -> dict:
        """Probe."""
        return {}

    with pytest.raises(ToolSignatureError) as caught:
        tool_schema(probe, name="probe")
    assert "a cookie" in str(caught.value)


def test_an_uploaded_file_parameter_is_refused_at_registration() -> None:
    async def probe(request, blob: Annotated[bytes, File()]) -> dict:
        """Probe."""
        return {}

    with pytest.raises(ToolSignatureError) as caught:
        tool_schema(probe, name="probe")
    assert "an uploaded file" in str(caught.value)


def test_a_dependency_is_refused_with_the_reason() -> None:
    from wreath.binding import Depends

    async def provide() -> int:
        return 1

    async def probe(request, count: int = Depends(provide)) -> dict:
        """Probe."""
        return {}

    with pytest.raises(ToolSignatureError) as caught:
        tool_schema(probe, name="probe")
    assert "Depends" in str(caught.value)


def test_a_tool_without_a_description_is_refused() -> None:
    async def undescribed(request) -> dict:
        return {}

    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(ValueError, match="needs a description"):
        mcp.tool(undescribed)


def test_a_synchronous_tool_is_refused() -> None:
    def blocking(request) -> dict:
        """Blocks."""
        return {}

    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(TypeError, match="async"):
        mcp.tool(blocking)


def test_a_duplicate_tool_name_is_refused() -> None:
    mcp = MCP(name="x", version="1.0.0")

    @mcp.tool(description="First.")
    async def probe(request) -> dict:
        return {}

    with pytest.raises(ValueError, match="already registered"):
        mcp.tool(probe, name="probe", description="Second.")
