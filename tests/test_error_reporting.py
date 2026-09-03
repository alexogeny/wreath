from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from wreath import Wreath
from wreath.client_facts import ClientFacts, ClientFactsProvider, UserAgentFacts, WreathGeoIP
from wreath.errors import (
    BugsnagErrorReporter,
    ErrorEvent,
    OtlpErrorReporter,
    RollbarErrorReporter,
    SentryErrorReporter,
)
from wreath.request import Request
from wreath.testing import TestClient


class Reporter:
    def __init__(self) -> None:
        self.events: list[ErrorEvent] = []

    async def report(self, event: ErrorEvent) -> None:
        self.events.append(event)


async def _receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request() -> Request:
    return Request({"type": "http", "headers": []}, _receive)


def test_sentry_reporter_validates_optional_client_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(capture_exception=lambda _error: None),
    )

    SentryErrorReporter()
    with pytest.raises(TypeError, match="client_facts.*resolve"):
        SentryErrorReporter(client_facts=object())


async def test_sentry_reporter_captures_directly_without_both_context_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Exception] = []
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(capture_exception=captured.append),
    )

    first = RuntimeError("no resolver")
    await SentryErrorReporter().report(ErrorEvent(first, _request(), "request"))

    class Resolver:
        def resolve(self, _request: Request) -> ClientFacts:
            raise AssertionError("a requestless event must not resolve facts")

    second = RuntimeError("no request")
    reporter = SentryErrorReporter(client_facts=Resolver())
    await reporter.report(ErrorEvent(second, None, "job"))

    assert captured == [first, second]


async def test_sentry_client_facts_require_scope_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(capture_exception=lambda _error: None),
    )

    class Resolver:
        def resolve(self, _request: Request) -> ClientFacts:
            return ClientFacts(None, UserAgentFacts(raw=""))

    reporter = SentryErrorReporter(client_facts=Resolver())
    with pytest.raises(RuntimeError, match="requires sentry-sdk with new_scope"):
        await reporter.report(ErrorEvent(RuntimeError("reported"), _request(), "request"))


def test_otlp_reporter_is_not_registered_twice_on_a_wreath_app() -> None:
    with pytest.raises(ValueError, match="automatic"):
        Wreath().add_error_reporter(OtlpErrorReporter())


async def test_unhandled_request_errors_reach_registered_reporters() -> None:
    app = Wreath()
    reporter = Reporter()
    app.add_error_reporter(reporter)

    @app.get("/broken")
    async def broken(request):
        raise RuntimeError("boom")

    async with TestClient(app) as client:
        response = await client.get("/broken")
    assert response.status == 500
    assert len(reporter.events) == 1
    assert reporter.events[0].phase == "request"
    assert isinstance(reporter.events[0].error, RuntimeError)


async def test_error_reporter_cancellation_unwinds_the_request() -> None:
    class CancelledReporter:
        async def report(self, event: ErrorEvent) -> None:
            raise asyncio.CancelledError

    app = Wreath()
    app.add_error_reporter(CancelledReporter())
    with pytest.raises(asyncio.CancelledError):
        await app._report_error(None, RuntimeError("boom"), phase="request")
    assert app.error_reporter_errors == 0


async def test_rollbar_and_bugsnag_are_first_class_reporters(monkeypatch) -> None:
    captured: list[tuple[str, object]] = []
    monkeypatch.setitem(
        sys.modules,
        "rollbar",
        SimpleNamespace(report_exc_info=lambda info: captured.append(("rollbar", info))),
    )
    monkeypatch.setitem(
        sys.modules,
        "bugsnag",
        SimpleNamespace(notify=lambda error: captured.append(("bugsnag", error))),
    )
    error = RuntimeError("reported")
    event = ErrorEvent(error=error, request=None, phase="job")
    await RollbarErrorReporter().report(event)
    await BugsnagErrorReporter().report(event)
    assert captured[0][0] == "rollbar"
    exception_info = captured[0][1]
    assert isinstance(exception_info, tuple)
    assert exception_info[1] is error
    assert captured[1] == ("bugsnag", error)


async def test_sentry_error_context_gets_bounded_client_facts(monkeypatch) -> None:
    captured: list[object] = []

    class Scope:
        def __init__(self) -> None:
            self.context: dict[str, object] = {}
            self.tags: dict[str, object] = {}

        def set_context(self, name: str, value: object) -> None:
            self.context[name] = value

        def set_tag(self, name: str, value: object) -> None:
            self.tags[name] = value

    scope = Scope()

    @contextmanager
    def new_scope():
        yield scope

    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(
            capture_exception=lambda error: captured.append(error),
            new_scope=new_scope,
        ),
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "client": ("4.1.1.1", None),
            "headers": [(b"user-agent", b"OAI-SearchBot/1.0")],
        },
        receive,
    )
    request._set_client(("4.1.1.1", None), source="forwarded")
    error = RuntimeError("reported")
    reporter = SentryErrorReporter(client_facts=ClientFactsProvider(geoip=WreathGeoIP()))
    await reporter.report(ErrorEvent(error, request, "request"))
    assert captured == [error]
    assert scope.context["wreath.client"] == {
        "user_agent.name": "OpenAI SearchBot",
        "user_agent.version": "1.0",
        "user_agent.synthetic.type": "bot",
        "wreath.client.agent.claimed": True,
        "wreath.client.agent.verified": False,
        "network.type": "ipv4",
        "wreath.client.address_source": "forwarded",
        "geo.country.iso_code": "US",
    }
    assert scope.tags["geo.country.iso_code"] == "US"


@pytest.mark.parametrize("mobile", [None, True])
async def test_sentry_context_omits_unknown_tags_and_preserves_known_mobile(
    monkeypatch: pytest.MonkeyPatch,
    mobile: bool | None,
) -> None:
    class Scope:
        def __init__(self) -> None:
            self.context: dict[str, object] = {}
            self.tags: dict[str, object] = {}

        def set_context(self, name: str, value: object) -> None:
            self.context[name] = value

        def set_tag(self, name: str, value: object) -> None:
            self.tags[name] = value

    scope = Scope()

    @contextmanager
    def new_scope():
        yield scope

    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(capture_exception=lambda _error: None, new_scope=new_scope),
    )

    facts = ClientFacts(None, UserAgentFacts(raw="", mobile=mobile))

    class Resolver:
        def resolve(self, _request: Request) -> ClientFacts:
            return facts

    reporter = SentryErrorReporter(client_facts=Resolver())
    await reporter.report(ErrorEvent(RuntimeError("reported"), _request(), "request"))

    expected = {} if mobile is None else {"browser.mobile": "true"}
    assert scope.tags == expected
