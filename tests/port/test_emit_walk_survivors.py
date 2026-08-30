from __future__ import annotations

import ast

import pytest

from wreath import port
from wreath._port.analyzer import _Imports
from wreath._port.emit.emitter import _Emitter


def _walk(source: str, *, settings: frozenset[str] = frozenset()) -> _Emitter:
    tree = ast.parse(source)
    emitter = _Emitter(source, _Imports().visit(tree))
    emitter.settings_models = settings
    emitter.visit(tree)
    return emitter


def _body(source: str, *, opinionated: bool = True) -> str:
    emitted = port.emit_module(source, opinionated=opinionated)
    return "\n".join(
        line for line in emitted.splitlines() if not line.startswith("# wreath-port:")
    )


def test_partial_model_family_closes_over_ancestors_and_descendants() -> None:
    source = (
        "class Plain(Generic[int]):\n"
        "    pass\n"
        "class Root(PartialModelMixin):\n"
        "    pass\n"
        "class Middle(Root):\n"
        "    pass\n"
        "class Patch(Middle.model_as_partial()):\n"
        "    pass\n"
        "class Leaf(Patch):\n"
        "    pass\n"
    )

    emitter = _walk(source)

    assert emitter._pydantic_partial_family == frozenset({"Root", "Middle", "Patch", "Leaf"})


def test_partial_model_detection_ignores_other_called_bases() -> None:
    source = (
        "class Factory(factory()):\n"
        "    pass\n"
        "class Converted(Factory.other_transform()):\n"
        "    pass\n"
        "class Patch(Factory.model_as_partial()):\n"
        "    pass\n"
    )

    emitter = _walk(source)

    assert emitter._pydantic_partial_family == frozenset({"Factory", "Patch"})


def test_timeout_constants_require_one_named_target_and_a_timeout_call() -> None:
    source = (
        "import httpx\n"
        "deadline = httpx.Timeout(3)\n"
        "annotated: object = httpx.Timeout(4)\n"
        "left = right = httpx.Timeout(5)\n"
        "holder.value = httpx.Timeout(6)\n"
        "not_timeout = other.Timeout(7)\n"
    )

    emitter = _walk(source)

    assert emitter._http_timeout_constants == {"deadline", "annotated"}


def test_transport_definitions_require_one_named_httpx_assignment() -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        "    first = second = httpx.AsyncHTTPTransport(retries=8)\n"
        "    holder.transport = httpx.AsyncHTTPTransport(retries=9)\n"
        "    async with httpx.AsyncClient(transport=first) as client:\n"
        "        return await client.get(url)\n"
        "async def foreign_fetch(url):\n"
        "    foreign = other.AsyncHTTPTransport(retries=10)\n"
        "    async with httpx.AsyncClient(transport=foreign) as client:\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert emitter._http_clients == {}
    assert emitter._http_retries == {}


def test_celery_task_runners_only_include_named_runner_task_decorators() -> None:
    source = (
        "from celery import Celery\n"
        "runner = Celery('jobs')\n"
        "other = object()\n"
        "@runner.task\n"
        "def plain():\n"
        "    pass\n"
        "@runner.task(name='async-job')\n"
        "async def async_job():\n"
        "    pass\n"
        "@other.task\n"
        "def foreign():\n"
        "    pass\n"
        "@factory.runner.task\n"
        "def nested_runner():\n"
        "    pass\n"
        "@runner.unrelated\n"
        "def unrelated():\n"
        "    pass\n"
    )

    emitter = _walk(source)

    assert emitter._celery_task_runners == {"plain": "runner", "async_job": "runner"}


@pytest.mark.parametrize(
    ("signature", "custom"),
    [
        ("self", False),
        ("self, value", True),
        ("self, /", True),
        ("self, *, value", True),
        ("self, *values", True),
        ("self, **values", True),
    ],
)
def test_settings_custom_init_recognizes_every_non_default_signature(
    signature: str, custom: bool
) -> None:
    source = (
        "class Settings:\n"
        f"    def __init__({signature}):\n"
        "        pass\n"
        "    def configure(self, value):\n"
        "        pass\n"
        "class Other:\n"
        "    def __init__(self, value):\n"
        "        pass\n"
    )

    emitter = _walk(source, settings=frozenset({"Settings"}))

    assert ("Settings" in emitter._settings_custom_init) is custom
    assert "Other" not in emitter._settings_custom_init


def test_settings_bindings_only_read_called_model_config_assignments() -> None:
    source = (
        "class Settings:\n"
        "    ignored = Config(env_file='wrong')\n"
        "    model_config: object = Config(env_file='.env', env_prefix='APP_')\n"
        "class Assigned:\n"
        "    model_config = Config(env_prefix='ASSIGNED_')\n"
        "class Bare:\n"
        "    model_config = {'env_file': '.wrong'}\n"
        "class Other:\n"
        "    model_config = Config(env_file='other')\n"
        "class IgnoredOnly:\n"
        "    ignored = Config(env_file='ignored')\n"
        "class OddTargets:\n"
        "    left = model_config = Config(env_file='wrong')\n"
        "    holder.model_config = Config(env_prefix='wrong')\n"
    )

    emitter = _walk(
        source,
        settings=frozenset({"Settings", "Assigned", "Bare", "IgnoredOnly", "OddTargets"}),
    )

    assert emitter._settings_bindings == {
        "Settings": ("'.env'", "'APP_'"),
        "Assigned": (None, "'ASSIGNED_'"),
        "Bare": (None, None),
        "IgnoredOnly": (None, None),
        "OddTargets": ("'wrong'", None),
    }


@pytest.mark.parametrize(
    "client",
    [
        "httpx.AsyncClient('https://example.test')",
        "httpx.AsyncClient(**options)",
        "httpx.AsyncClient(verify=False)",
        "other.AsyncClient()",
    ],
)
def test_ineligible_http_clients_are_not_registered(client: str) -> None:
    source = (
        "import httpx\n"
        "async def fetch():\n"
        f"    async with {client} as session:\n"
        "        return await session.get('/items')\n"
    )

    emitter = _walk(source)

    assert emitter._http_clients == {}
    assert emitter._http_requests == {}


def test_http_client_optional_target_must_be_a_name() -> None:
    source = (
        "import httpx\n"
        "async def fetch():\n"
        "    async with httpx.AsyncClient() as (client, extra):\n"
        "        return await client.get('/items')\n"
    )

    emitter = _walk(source)

    assert emitter._http_clients == {}


def test_dynamic_http_request_uses_the_method_specific_url_argument() -> None:
    source = (
        "import httpx\n"
        "async def fetch(method, url):\n"
        "    async with httpx.AsyncClient(headers={'x': 'y'}) as client:\n"
        "        response = await client.request(method, url, timeout=9)\n"
        "        return response\n"
    )
    tree = ast.parse(source)
    request = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "request"
    )
    emitter = _Emitter(source, _Imports().visit(tree))

    emitter.visit(tree)

    client_key = next(iter(emitter._http_clients))
    assert ast.unparse(emitter._http_dynamic_clients[client_key]) == "url"
    assert ast.unparse(emitter._http_request_timeouts[client_key]) == "9"
    assert emitter._http_requests[id(request)] == client_key


def test_dynamic_http_client_requires_exactly_one_supported_request() -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient() as none:\n"
        "        pass\n"
        "    async with httpx.AsyncClient() as many:\n"
        "        await many.get(url)\n"
        "        await many.post(url)\n"
        "    async with httpx.AsyncClient() as missing:\n"
        "        await missing.request('GET')\n"
        "    async with httpx.AsyncClient() as unsupported:\n"
        "        await unsupported.get(url, follow_redirects=True)\n"
    )

    emitter = _walk(source)

    assert emitter._http_dynamic_clients == {}


@pytest.mark.parametrize(
    "transport",
    [
        "httpx.AsyncHTTPTransport(1)",
        "httpx.AsyncHTTPTransport(retries=2, verify=False)",
        "other.AsyncHTTPTransport(retries=2)",
        "object()",
    ],
)
def test_ineligible_http_transports_reject_the_client(transport: str) -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        f"    transport = {transport}\n"
        "    async with httpx.AsyncClient(transport=transport) as client:\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert emitter._http_clients == {}
    assert emitter._http_retries == {}
    assert emitter._http_transport_assignments == set()


def test_transport_lookup_obeys_order_and_callable_scope() -> None:
    source = (
        "import httpx\n"
        "transport = httpx.AsyncHTTPTransport(retries=1)\n"
        "async def outer(first, second):\n"
        "    transport = httpx.AsyncHTTPTransport(retries=2)\n"
        "    async with httpx.AsyncClient(transport=transport) as client:\n"
        "        await client.get(first)\n"
        "    async def inner():\n"
        "        transport = httpx.AsyncHTTPTransport(retries=3)\n"
        "        async with httpx.AsyncClient(transport=transport) as client:\n"
        "            return await client.get(second)\n"
        "    transport = httpx.AsyncHTTPTransport(retries=99)\n"
        "    return await inner()\n"
    )

    emitter = _walk(source)

    assert sorted(ast.unparse(value) for value in emitter._http_retries.values()) == ["2", "3"]
    assert len(emitter._http_transport_assignments) == 2


def test_inline_transport_records_retries_without_an_assignment() -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient(\n"
        "        transport=httpx.AsyncHTTPTransport(retries=4)\n"
        "    ) as client:\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert [ast.unparse(value) for value in emitter._http_retries.values()] == ["4"]
    assert emitter._http_transport_assignments == set()


def test_transport_without_retries_is_eligible_without_retry_state() -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient(\n"
        "        transport=httpx.AsyncHTTPTransport()\n"
        "    ) as client:\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert len(emitter._http_clients) == 1
    assert emitter._http_retries == {}
    assert emitter._http_transport_assignments == set()


def test_transport_lookup_does_not_capture_a_module_definition_from_a_function() -> None:
    source = (
        "import httpx\n"
        "transport = httpx.AsyncHTTPTransport(retries=7)\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient(transport=transport) as client:\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert emitter._http_clients == {}
    assert emitter._http_retries == {}


def test_inline_foreign_transport_is_not_eligible() -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient(\n"
        "        transport=other.AsyncHTTPTransport(retries=7)\n"
        "    ) as client:\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert emitter._http_clients == {}


def test_http_response_names_keep_their_sync_and_async_owner() -> None:
    source = (
        "import httpx\n"
        "async def outer(url):\n"
        "    async with httpx.AsyncClient(base_url='https://example.test') as client:\n"
        "        response = await client.get(url)\n"
        "        left = right = await client.post(url)\n"
        "        holder.value = await client.put(url)\n"
        "        pending = await ready\n"
        "        foreign = await other()\n"
    )
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef))
    emitter = _Emitter(source, _Imports().visit(tree))

    emitter.visit(tree)

    assert emitter._http_responses == {
        (id(function), "response"),
        (id(function), "left"),
        (id(function), "right"),
    }


def test_module_level_http_response_has_no_callable_owner() -> None:
    source = (
        "import httpx\n"
        "async with httpx.AsyncClient(base_url='https://example.test') as client:\n"
        "    response = await client.get('/status')\n"
    )

    emitter = _walk(source)

    assert emitter._http_responses == {(None, "response")}


def test_http_request_collection_rejects_other_receivers_and_methods() -> None:
    source = (
        "import httpx\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient(\n"
        "        base_url='https://example.test', headers={'x': 'client'}\n"
        "    ) as client:\n"
        "        await other.get(url)\n"
        "        await client.close()\n"
        "        return await client.get(url)\n"
    )

    emitter = _walk(source)

    assert len(emitter._http_requests) == 1
    key = next(iter(emitter._http_clients))
    _, headers = emitter._http_clients[key]
    assert headers is not None
    assert ast.unparse(headers) == "{'x': 'client'}"


def test_test_client_discovery_distinguishes_globals_and_fixtures() -> None:
    source = (
        "import pytest\n"
        "from fastapi.testclient import TestClient\n"
        "global_client = TestClient(app)\n"
        "first = second = TestClient(app)\n"
        "holder.client = TestClient(app)\n"
        "@pytest.fixture()\n"
        "def returned():\n"
        "    return TestClient(app)\n"
        "@custom.fixture\n"
        "async def yielded():\n"
        "    yield TestClient(app)\n"
        "@pytest.mark.fixture\n"
        "def not_a_fixture():\n"
        "    return TestClient(app)\n"
        "def undecorated():\n"
        "    return TestClient(app)\n"
        "@pytest.fixture\n"
        "def wrong_return():\n"
        "    return other()\n"
        "@pytest.fixture\n"
        "def bare_return():\n"
        "    return value\n"
        "@custom.not_fixture\n"
        "def wrong_decorator():\n"
        "    return TestClient(app)\n"
    )

    emitter = _walk(source)

    assert set(emitter._global_test_clients) == {"global_client"}
    assert emitter._fixture_test_clients == {"returned", "yielded", "not_a_fixture"}


def test_import_injection_stops_at_the_top_level_import_prologue() -> None:
    source = (
        '"module docs"\n'
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "def route():\n"
        "    return JSONResponse({'ok': True})\n"
        "from fastapi.responses import JSONResponse\n"
    )

    body = _body(source)

    assert body.index("from wreath import Wreath") < body.index("app = Wreath()")
    assert body.index("from wreath import JSONResponse") < body.index("app = Wreath()")


def test_expression_statement_ends_the_import_prologue_unless_it_is_a_constant() -> None:
    source = (
        "from fastapi import FastAPI\n"
        "configure()\n"
        "from fastapi.responses import JSONResponse\n"
        "app = FastAPI()\n"
    )

    body = _body(source)

    assert body.index("from wreath import JSONResponse") < body.index("configure()")


@pytest.mark.parametrize(
    ("source", "present"),
    [
        ("from .fastapi import FastAPI\nvalue = FastAPI\n", "from .fastapi import FastAPI"),
        (
            "from .fastapi.exceptions import HTTPException\nvalue = HTTPException\n",
            "from .fastapi.exceptions import HTTPException",
        ),
        (
            "from .pydantic_settings import BaseSettings\nvalue = BaseSettings\n",
            "from .pydantic_settings import BaseSettings",
        ),
        (
            "from .pydantic_partial import PartialModelMixin\nvalue = PartialModelMixin\n",
            "from .pydantic_partial import PartialModelMixin",
        ),
    ],
)
def test_relative_imports_are_not_treated_as_external_framework_imports(
    source: str, present: str
) -> None:
    assert present in _body(source)


def test_custom_http_exception_import_is_not_rewritten() -> None:
    source = "from custom import HTTPException, Other\nerror = HTTPException\nother = Other\n"

    assert "from custom import HTTPException, Other" in _body(source)


def test_httpx_import_retention_is_per_bound_name_and_preserves_aliases() -> None:
    source = (
        "from httpx import AsyncClient as Client, Timeout, Limits as Bounds, HTTPError as Error\n"
        "from other import AsyncClient\n"
        "kept = Timeout\n"
        "bounds = Bounds\n"
        "error = Error\n"
        "async def fetch(url):\n"
        "    async with Client(base_url='https://example.test') as client:\n"
        "        return await client.get(url)\n"
        "foreign = AsyncClient\n"
    )

    body = _body(source)

    assert "from httpx import Timeout, Limits as Bounds" in body
    assert "AsyncClient as Client" not in body
    assert "from other import AsyncClient" in body


def test_plain_httpx_import_retention_handles_mixed_imports_and_aliases() -> None:
    source = (
        "import os, httpx, httpx as transport\n"
        "kept = transport.Timeout\n"
        "async def fetch(url):\n"
        "    async with httpx.AsyncClient(base_url='https://example.test') as client:\n"
        "        return await client.get(url)\n"
    )

    body = _body(source)

    assert "import os, httpx as transport" in body
    assert "import os, httpx," not in body


def test_optional_model_imports_drop_only_rewritten_bound_names() -> None:
    source = (
        "from pydantic import BaseModel\n"
        "from pydantic_settings import BaseSettings as Settings, SettingsConfigDict, "
        "Other, Unused\n"
        "from pydantic_partial import PartialModelMixin as Partial, OtherPartial, Sibling\n"
        "class AppSettings(Settings):\n"
        "    model_config = SettingsConfigDict(env_prefix='APP_')\n"
        "class Kept(Partial, BaseModel):\n"
        "    pass\n"
        "other = Other\n"
        "other_partial = OtherPartial\n"
    )

    body = _body(source)

    assert "from pydantic_settings import Other, Unused" in body
    assert "from pydantic_partial import OtherPartial, Sibling" in body
    assert "BaseSettings as Settings" not in body
    assert "PartialModelMixin as Partial" not in body
    assert "SettingsConfigDict" not in body


def test_live_partial_model_alias_is_retained() -> None:
    source = (
        "from pydantic_partial import PartialModelMixin as Partial, Other\n"
        "callback = Partial\n"
        "other = Other\n"
    )

    assert "from pydantic_partial import PartialModelMixin as Partial, Other" in _body(source)


def test_optional_model_liveness_handles_module_attributes() -> None:
    source = "import pydantic_settings as settings\nvalue = settings.Other\n"

    assert "import pydantic_settings as settings" in _body(source)


def test_middleware_import_removal_is_name_specific() -> None:
    source = (
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.cors import CORSMiddleware, OtherMiddleware\n"
        "from starlette.middleware.trustedhost import TrustedHostMiddleware as Trusted, Extra\n"
        "app = FastAPI()\n"
        "app.add_middleware(CORSMiddleware, allow_origins=['*'])\n"
        "app.add_middleware(Trusted, allowed_hosts=['*'])\n"
        "other = OtherMiddleware\n"
        "extra = Extra\n"
    )

    body = _body(source)

    assert "CORSMiddleware" not in body
    assert "TrustedHostMiddleware" not in body
    assert "from fastapi.middleware.cors import OtherMiddleware" in body
    assert "from starlette.middleware.trustedhost import Extra" in body


def test_similarly_named_custom_middleware_import_is_untouched() -> None:
    source = (
        "from custom.middleware import CORSMiddleware, Other\n"
        "cors = CORSMiddleware\n"
        "other = Other\n"
    )

    assert "from custom.middleware import CORSMiddleware, Other" in _body(source)


def test_removed_custom_middleware_import_keeps_unrelated_aliases() -> None:
    source = (
        "from middleware.ratelimiter import (\n"
        "    InMemoryLimitProvider, RateLimitingMiddleware, Other as Kept\n"
        ")\n"
        "def configure(app):\n"
        "    app.add_middleware(\n"
        "        RateLimitingMiddleware, included_routes=['/'],\n"
        "        provider=InMemoryLimitProvider(timespan=60, limit=2),\n"
        "    )\n"
        "callback = RateLimitingMiddleware\n"
        "kept = Kept\n"
    )

    body = _body(source)

    assert (
        "from middleware.ratelimiter import RateLimitingMiddleware, Other as Kept" in body
    )
    assert "InMemoryLimitProvider" not in body
    assert "callback = RateLimitingMiddleware" in body


def test_test_client_import_is_swapped_to_wreath() -> None:
    source = (
        "from starlette.testclient import TestClient\n"
        "client = TestClient(app)\n"
    )

    body = _body(source)

    assert "from starlette.testclient" not in body
    assert "from wreath.testing import TestClient" in body


def test_cachetools_import_removes_only_rewritten_cache_classes() -> None:
    source = (
        "from cachetools import TTLCache, cached, Other\n"
        "cache = TTLCache(maxsize=8, ttl=30)\n"
        "decorator = cached\n"
        "other = Other\n"
    )

    body = _body(source)

    assert "from cachetools import cached, Other" in body
    assert "TTLCache" not in body


def test_arrow_and_strawberry_imports_only_drop_when_every_binding_is_rewritten() -> None:
    source = (
        "import os, arrow\n"
        "import strawberry as berries\n"
        "kept_arrow = arrow\n"
        "kept_strawberry = berries\n"
    )

    body = _body(source)

    assert "import os, arrow" in body
    assert "import strawberry as berries" in body


@pytest.mark.parametrize("module", ["arrow", "strawberry"])
def test_unaliased_retained_optional_import_is_not_dropped(module: str) -> None:
    source = f"import {module}\nkept = {module}\n"

    assert f"import {module}" in _body(source)


def test_jsonable_encoder_removal_keeps_leftovers_and_live_references() -> None:
    rewritten = _body(
        "from fastapi.encoders import jsonable_encoder, other\n"
        "value = jsonable_encoder({'x': 1})\n"
        "kept = other\n"
    )
    retained = _body(
        "from fastapi.encoders import jsonable_encoder, other\n"
        "callback = jsonable_encoder\n"
        "kept = other\n"
    )

    assert "from fastapi.encoders import other" in rewritten
    assert "jsonable_encoder" not in rewritten
    assert "from fastapi.encoders import jsonable_encoder, other" in retained


def test_django_import_removal_is_per_bound_name_and_preserves_live_aliases() -> None:
    source = (
        "from django.db.models import Model as DjangoModel, CharField, Other as Kept\n"
        "class Item(DjangoModel):\n"
        "    name = CharField(max_length=20)\n"
        "kept = Kept\n"
    )

    body = _body(source)

    assert "from django.db.models import Other as Kept" in body
    assert "DjangoModel" not in body
    assert "CharField" not in body
    assert "kept = Kept" in body
