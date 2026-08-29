"""Application-owned error reporting with vendor and OTLP adapters."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from . import logging as log
from .request import Request

if TYPE_CHECKING:
    from .client_facts import ClientFacts

__all__ = [
    "ErrorEvent",
    "ErrorReporter",
    "BugsnagErrorReporter",
    "OtlpErrorReporter",
    "RollbarErrorReporter",
    "SentryErrorReporter",
    "record_error",
]

_ERROR = log.event(
    "wreath.error.unhandled",
    "unhandled {error_type} at {phase}",
    level=log.ERROR,
    fields=(
        log.field("error_type", str, log.RAW),
        log.field("phase", str, log.RAW),
    ),
)


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """One failure at a named framework boundary.

    Request bodies and headers are deliberately absent. A reporter may inspect
    the request it is explicitly handed, but Wreath never exports credentials or
    payloads merely because reporting was enabled.
    """

    error: Exception
    request: Request | None
    phase: str


@runtime_checkable
class ErrorReporter(Protocol):
    """An async, application-owned error sink."""

    async def report(self, event: ErrorEvent) -> None: ...


class _ClientFactsResolver(Protocol):
    def resolve(self, request: Request) -> ClientFacts: ...


def record_error(event: ErrorEvent) -> None:
    """Publish an exception marker onto Wreath's existing OTLP/logging spine.

    The record is correlated by the active Flight Recorder request context. It
    carries type and phase, not the exception message, request headers, or body;
    those frequently contain secrets. OTLP log export, WFR1 retention, and any
    other configured log sink all consume this same record.
    """
    _ERROR(type(event.error).__name__, event.phase)


class OtlpErrorReporter:
    """Explicit reporter form of `record_error` for external pipelines.

    Wreath applications do not need to register this: the application boundary
    records errors automatically. It exists for job runners and other owners
    that use the same `ErrorReporter` protocol outside request dispatch.
    """

    async def report(self, event: ErrorEvent) -> None:
        record_error(event)


class SentryErrorReporter:
    """Forward errors to an explicitly initialized `sentry-sdk` installation.

    This adapter does not call `sentry_sdk.init` and therefore owns no hidden
    process configuration. The application initializes Sentry with its DSN and
    lifecycle choices, then registers this reporter on its Wreath instance.
    """

    __slots__ = ("_capture", "_client_facts", "_new_scope")

    def __init__(self, *, client_facts: _ClientFactsResolver | None = None) -> None:
        try:
            sentry_sdk = importlib.import_module("sentry_sdk")
        except ImportError as exc:
            raise RuntimeError(
                "SentryErrorReporter requires the optional 'sentry-sdk' package; "
                "install sentry-sdk and initialize it before registration"
            ) from exc
        if client_facts is not None and not callable(getattr(client_facts, "resolve", None)):
            raise TypeError("Sentry client_facts must expose resolve(request)")
        self._capture = sentry_sdk.capture_exception
        self._client_facts = client_facts
        self._new_scope = getattr(sentry_sdk, "new_scope", None)

    async def report(self, event: ErrorEvent) -> None:
        if event.request is None or self._client_facts is None:
            self._capture(event.error)
            return
        if not callable(self._new_scope):
            raise RuntimeError("Sentry client-facts context requires sentry-sdk with new_scope()")
        from .client_facts import client_fact_attributes

        facts = self._client_facts.resolve(event.request)
        attributes = client_fact_attributes(facts)
        with self._new_scope() as scope:
            scope.set_context("wreath.client", attributes)
            for name in (
                "geo.country.iso_code",
                "network.type",
                "user_agent.synthetic.type",
                "wreath.client.address_source",
            ):
                value = attributes.get(name)
                if value is not None:
                    scope.set_tag(name, value)
            mobile = attributes.get("browser.mobile")
            if mobile is not None:
                scope.set_tag("browser.mobile", str(mobile).lower())
            self._capture(event.error)


class _OptionalErrorReporter:
    """Load one callback from an application-configured optional integration."""

    __slots__ = ("_callback",)

    _attribute: str
    _error: str
    _module: str

    def __init__(self) -> None:
        try:
            integration = importlib.import_module(self._module)
        except ImportError as exc:
            raise RuntimeError(self._error) from exc
        self._callback = getattr(integration, self._attribute)


class RollbarErrorReporter(_OptionalErrorReporter):
    """Forward errors to an application-configured `rollbar` installation."""

    __slots__ = ()

    _attribute = "report_exc_info"
    _error = (
        "RollbarErrorReporter requires the optional 'rollbar' package; "
        "install and initialize rollbar before registration"
    )
    _module = "rollbar"

    async def report(self, event: ErrorEvent) -> None:
        self._callback((type(event.error), event.error, event.error.__traceback__))


class BugsnagErrorReporter(_OptionalErrorReporter):
    """Forward errors to an application-configured `bugsnag` installation."""

    __slots__ = ()

    _attribute = "notify"
    _error = (
        "BugsnagErrorReporter requires the optional 'bugsnag' package; "
        "install and configure bugsnag before registration"
    )
    _module = "bugsnag"

    async def report(self, event: ErrorEvent) -> None:
        self._callback(event.error)
