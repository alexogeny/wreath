from __future__ import annotations

import base64
import importlib
import json
import sys

import pytest

from wreath import Wreath
from wreath._cli import main
from wreath._port.verify import RequestCase, load_cases, verify_apps


def _app(body: str, *, header: str = "same") -> Wreath:
    app = Wreath()

    @app.get("/value")
    async def value(request):
        from wreath.response import Response

        return Response(
            body.encode(),
            headers=[(b"x-contract", header.encode())],
        )

    return app


@pytest.mark.asyncio
async def test_two_apps_are_compared_through_lifespan_and_http_semantics() -> None:
    report = await verify_apps(
        _app("same"),
        _app("same"),
        (RequestCase("value", "GET", "/value"),),
    )
    assert report.equivalent
    assert report.as_dict() == {"cases": 1, "equivalent": True, "differences": []}


@pytest.mark.asyncio
async def test_every_different_response_field_is_named() -> None:
    report = await verify_apps(
        _app("source", header="one"),
        _app("candidate", header="two"),
        (RequestCase("value", "GET", "/value"),),
    )
    assert not report.equivalent
    assert {difference.field for difference in report.differences} == {
        "headers",
        "body_base64",
    }
    assert "value: headers" in report.render_text()
    assert base64.b64encode(b"source").decode() in report.render_text()


@pytest.mark.asyncio
async def test_repeated_response_header_value_order_remains_observable() -> None:
    def app(values: tuple[bytes, bytes]) -> Wreath:
        application = Wreath()

        @application.get("/")
        async def home(request):
            from wreath.response import Response

            return Response(headers=[(b"set-cookie", value) for value in values])

        return application

    report = await verify_apps(
        app((b"one=1", b"two=2")),
        app((b"two=2", b"one=1")),
        (RequestCase("cookies", "GET", "/"),),
    )
    assert [difference.field for difference in report.differences] == ["headers"]


def test_the_json_corpus_refuses_ambiguous_bodies(tmp_path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "create",
                        "method": "POST",
                        "path": "/items",
                        "body": "{}",
                        "body_base64": "e30=",
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="body or body_base64, not both"):
        load_cases(path)


def test_request_cases_refuse_headers_the_test_client_cannot_preserve() -> None:
    with pytest.raises(ValueError, match="repeats request header"):
        RequestCase(
            "duplicate",
            "GET",
            "/",
            (("accept", "application/json"), ("Accept", "text/plain")),
        )


def test_the_cli_exits_one_and_names_a_runtime_difference(tmp_path, monkeypatch, capsys) -> None:
    module = tmp_path / "port_verify_apps.py"
    module.write_text(
        "from wreath import Wreath\n"
        "source = Wreath()\n"
        "candidate = Wreath()\n"
        "@source.get('/')\n"
        "async def source_value(request): return 'source'\n"
        "@candidate.get('/')\n"
        "async def candidate_value(request): return 'candidate'\n"
    )
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"name": "home", "method": "GET", "path": "/"}]))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    try:
        code = main(
            [
                "port",
                "--verify",
                "port_verify_apps:source",
                "port_verify_apps:candidate",
                "--cases",
                str(cases),
            ]
        )
    finally:
        sys.modules.pop("port_verify_apps", None)
    assert code == 1
    assert "home: body_base64" in capsys.readouterr().out
