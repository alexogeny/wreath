from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

from wreath import Wreath
from wreath._audit.model import Finding, Severity
from wreath.hardening import (
    HardeningError,
    application_sources,
    apply_policy,
    audit_application,
    audit_configuration,
    check_application,
    resolve_policy,
)

#: `tests/conftest.py` turns the startup audit off for every other test by
#: setting `WREATH_HARDENING=off`, which outranks `hardening=`. This file is the
#: one that asserts what each policy *does*, so it keeps the real default --
#: the exemption is stated here, in the file that needs it, rather than as a
#: list of filenames somewhere a reader of this file would never look.
pytestmark = pytest.mark.hardening

DEFECTIVE = '''
"""A handler with one planted defect, for the boot tests."""
from __future__ import annotations

from wreath import Router

freight = Router(prefix="/shipments")


@freight.get("/search")
async def search(request, db, q: str = ""):
    sql = f"SELECT id FROM shipments WHERE reference ILIKE \'%{q}%\'"
    return await db.raw(sql).fetch()
'''

CLEAN = '''
"""The same handler with the defect removed -- one character, at the quote."""
from __future__ import annotations

from wreath import Router

freight = Router(prefix="/shipments")


@freight.get("/search")
async def search(request, db, q: str = ""):
    pattern = f"%{q}%"
    return await db.raw(t"SELECT id FROM shipments WHERE reference ILIKE {pattern}").fetch()
'''


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build an app whose handler lives in a module on disk.

    On disk and genuinely imported, because that is the only way
    `application_sources` can find anything: it resolves a handler's
    `__module__` through `sys.modules` to a file. A handler defined in this test
    module would resolve to this test module, which is not what is under test.
    """

    def build(source: str, name: str, **kwargs):
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "routes.py").write_text(source, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        module = importlib.import_module(f"{name}.routes")
        monkeypatch.setitem(sys.modules, f"{name}.routes", module)
        app = Wreath(**kwargs)
        app.get("/search")(module.search)
        return app

    return build


def test_the_default_policy_is_warn() -> None:
    assert Wreath()._hardening == "warn"


def test_the_environment_overrides_the_requested_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_HARDENING", "block")
    assert resolve_policy("warn") == "block"
    assert Wreath(hardening="warn")._hardening == "block"


def test_an_unknown_policy_is_refused_rather_than_defaulted() -> None:
    # Neither direction is safe to guess: falling back to `off` hides findings,
    # and falling back to `block` refuses to start. Say so instead.
    with pytest.raises(ValueError, match="not a hardening policy"):
        resolve_policy("strict")


def test_an_unknown_policy_is_refused_when_the_application_is_built() -> None:
    with pytest.raises(ValueError, match="not a hardening policy"):
        Wreath(hardening="on")


def test_an_unknown_policy_in_the_environment_names_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_HARDENING", "yes")
    with pytest.raises(ValueError, match="WREATH_HARDENING"):
        resolve_policy("warn")


def _error() -> Finding:
    return Finding(
        rule_id="sql-interpolation",
        severity=Severity.ERROR,
        surface="shop/routes.py",
        message="built by interpolation",
        location="7:11",
    )


def _warning() -> Finding:
    return Finding(
        rule_id="case-mapped-authz",
        severity=Severity.WARN,
        surface="shop/routes.py",
        message="case mapped",
        location="12:4",
    )


def test_warn_logs_every_finding_and_returns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        reported = apply_policy([_error()], "warn")
    assert len(reported) == 1
    assert "shop/routes.py:7:11" in caplog.text
    assert "sql-interpolation" in caplog.text


def test_block_raises_and_carries_every_error() -> None:
    findings = [_error(), _error()]
    with pytest.raises(HardeningError) as raised:
        apply_policy(findings, "block")
    assert len(raised.value.findings) == 2
    assert "sql-interpolation" in str(raised.value)


def test_block_does_not_refuse_over_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    # A WARN rule is one whose correct form is a judgement call. Refusing to
    # start over a judgement call is how `block` stops being a setting anybody
    # is willing to turn on.
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        reported = apply_policy([_warning()], "block")
    assert len(reported) == 1
    assert "case-mapped-authz" in caplog.text


def test_block_still_reports_the_warnings_beside_the_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        with pytest.raises(HardeningError):
            apply_policy([_error(), _warning()], "block")
    assert "case-mapped-authz" in caplog.text


def test_off_reports_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        assert apply_policy([_error()], "off") == (_error(),)
    assert caplog.text == ""


def test_block_with_no_findings_does_not_raise() -> None:
    assert apply_policy([], "block") == ()


def test_warn_with_no_findings_says_nothing_at_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A clean application must boot silently. A framework that logs "0 findings"
    # on every start is one whose output people filter out, and then the line
    # that matters is filtered out with it.
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        assert apply_policy([], "warn") == ()
    assert caplog.text == ""


def test_an_error_is_logged_at_error_level_and_a_warning_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        apply_policy([_error(), _warning()], "warn")
    levels = {
        record.levelno for record in caplog.records if "sql-interpolation" in record.getMessage()
    }
    assert levels == {logging.ERROR}
    levels = {
        record.levelno for record in caplog.records if "case-mapped-authz" in record.getMessage()
    }
    assert levels == {logging.WARNING}


def test_the_summary_line_counts_one_finding_in_the_singular(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        apply_policy([_error()], "warn")
    assert "1 finding above" in caplog.text


def test_the_summary_line_counts_several_in_the_plural(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        apply_policy([_error(), _warning()], "warn")
    assert "2 findings above" in caplog.text


def test_blocking_does_not_also_log_the_errors_it_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # They are in the exception, which is about to be printed by whatever
    # handles a failed startup. Logging them as well means every finding is
    # reported twice and the reader has to work out whether that is two
    # findings or one.
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        with pytest.raises(HardeningError):
            apply_policy([_error()], "block")
    assert "sql-interpolation" not in caplog.text


def test_blocking_over_warnings_alone_does_not_suggest_turning_block_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        apply_policy([_warning()], "block")
    assert "hardening='block'" not in caplog.text


def test_the_error_message_counts_one_finding_in_the_singular() -> None:
    with pytest.raises(HardeningError, match="1 finding\n"):
        apply_policy([_error()], "block")


def test_the_error_message_counts_several_in_the_plural() -> None:
    with pytest.raises(HardeningError, match="2 findings"):
        apply_policy([_error(), _error()], "block")


def test_a_bad_policy_written_in_code_names_the_parameter() -> None:
    # The mirror of the environment case: the message has to send the reader to
    # the place the value actually came from.
    with pytest.raises(ValueError, match="hardening='strict'"):
        resolve_policy("strict")


def test_the_application_package_is_what_is_scanned(application) -> None:
    app = application(DEFECTIVE, "shop_scanned")
    roots = application_sources(app)
    assert [root.name for root in roots] == ["shop_scanned"]
    assert roots[0].is_dir()


def test_wreath_is_never_scanned() -> None:
    # An application cannot fix a finding in the framework, so reporting one is
    # noise it has no move against.
    app = Wreath()
    app.health()
    assert application_sources(app) == []


def test_an_application_with_no_routes_scans_nothing() -> None:
    assert application_sources(Wreath()) == []


def _with_endpoints(*endpoints) -> Wreath:
    """An app whose route table holds exactly these endpoints.

    Synthetic rather than registered, because every case below is a handler
    whose *provenance* is unusual -- defined in `__main__`, installed into site
    packages, or belonging to a module that is no longer imported -- and none of
    those can be produced by writing an ordinary route.
    """
    from types import SimpleNamespace

    app = Wreath()
    app._routes.extend(SimpleNamespace(endpoint=endpoint) for endpoint in endpoints)
    return app


def test_a_top_level_module_contributes_only_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its "package root" is whatever directory it happens to sit in -- a scripts
    # folder, a home directory, a test suite -- and walking that is somewhere
    # between wasteful and wrong.
    (tmp_path / "solo.py").write_text("def handler():\n    return None\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("solo")
    monkeypatch.setitem(sys.modules, "solo", module)
    roots = application_sources(_with_endpoints(module.handler))
    assert roots == [tmp_path / "solo.py"]


def test_a_handler_defined_in_main_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `__main__.__file__` is the script that was run, and its directory is the
    # working directory -- which is a scripts folder or a home directory far
    # more often than it is the application.
    # `__main__` is given a real `__file__` on purpose: under pytest it has one
    # that points into the runner, so a test that did not set it would pass
    # whether or not the guard is there.
    script = tmp_path / "serve.py"
    script.write_text("def handler():\n    return None\n")
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(script), raising=False)

    def handler() -> None:
        return None

    handler.__module__ = "__main__"
    assert application_sources(_with_endpoints(handler)) == []


def test_a_handler_with_no_module_is_skipped() -> None:
    # Skipped by the same guard as a module that is not imported: an empty name
    # finds nothing in `sys.modules`, and nothing has no `__file__`.
    handler = _with_endpoints  # any callable
    original = handler.__module__
    try:
        handler.__module__ = ""
        assert application_sources(_with_endpoints(handler)) == []
    finally:
        handler.__module__ = original


def test_a_handler_whose_module_is_not_imported_is_skipped() -> None:
    def handler() -> None:
        return None

    handler.__module__ = "a_module_that_was_never_imported"
    assert application_sources(_with_endpoints(handler)) == []


def test_a_handler_installed_into_site_packages_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dependency's defect is not this application's to fix, and reporting one
    # at every boot is noise it has no move against.
    site = tmp_path / "site-packages" / "vendored"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("")
    (site / "routes.py").write_text("def handler():\n    return None\n")
    monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
    module = importlib.import_module("vendored.routes")
    monkeypatch.setitem(sys.modules, "vendored.routes", module)
    assert application_sources(_with_endpoints(module.handler)) == []


def test_a_wrapped_handler_resolves_to_the_module_it_was_written_in(
    application,
) -> None:
    # `@authenticated()` and friends wrap the endpoint. A wrapper defined in
    # wreath would resolve to wreath's own tree, which is excluded -- so the
    # application would silently scan nothing at all.
    from functools import wraps

    app = application(DEFECTIVE, "shop_wrapped")
    inner = app._routes[0].endpoint

    @wraps(inner)
    async def wrapper(*args, **kwargs):  # pragma: no cover - never called
        return await inner(*args, **kwargs)

    wrapper.__module__ = "wreath.auth"
    app._routes[0] = type(app._routes[0])(
        **{
            **{f: getattr(app._routes[0], f) for f in app._routes[0].__dataclass_fields__},
            "endpoint": wrapper,
        }
    )
    assert [root.name for root in application_sources(app)] == ["shop_wrapped"]


def test_the_defect_is_found_through_the_application(application) -> None:
    app = application(DEFECTIVE, "shop_found")
    rules = {finding.rule_id for finding in audit_application(app).findings}
    assert "sql-interpolation" in rules


def test_the_corrected_handler_produces_nothing(application) -> None:
    # The t-string spelling of the same handler. This is the assertion that
    # says the remedy the finding names actually clears the finding.
    app = application(CLEAN, "shop_clean")
    assert audit_application(app).findings == []


def test_block_refuses_an_application_carrying_a_defect(application) -> None:
    app = application(DEFECTIVE, "shop_blocked", hardening="block")
    with pytest.raises(HardeningError, match="sql-interpolation"):
        check_application(app, "block")


def test_warn_starts_it_and_says_so(application, caplog: pytest.LogCaptureFixture) -> None:
    app = application(DEFECTIVE, "shop_warned", hardening="warn")
    with caplog.at_level(logging.WARNING, logger="wreath.hardening"):
        reported = check_application(app, "warn")
    assert any(finding.rule_id == "sql-interpolation" for finding in reported)
    assert "hardening='block'" in caplog.text


def test_off_scans_nothing_at_all(application) -> None:
    app = application(DEFECTIVE, "shop_off", hardening="off")
    assert check_application(app, "off") == ()


def test_block_starts_a_clean_application(application) -> None:
    app = application(CLEAN, "shop_ok", hardening="block")
    assert check_application(app, "block") == ()


@pytest.mark.asyncio
async def test_lifespan_startup_fails_under_block(application) -> None:
    app = application(DEFECTIVE, "shop_lifespan", hardening="block")
    sent: list[dict] = []
    incoming = iter(({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}))

    async def receive() -> dict:
        return next(incoming)

    async def send(message: dict) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    replies = [message["type"] for message in sent]
    assert "lifespan.startup.complete" not in replies
    failure = next(m for m in sent if m["type"] == "lifespan.startup.failed")
    assert "sql-interpolation" in failure["message"]


class _Policy:
    allow_private = True
    allow_loopback = True
    allow_link_local = False


class _Client:
    _destination = _Policy()


def test_a_widened_destination_policy_is_found_at_boot() -> None:
    # The half a source rule structurally cannot see: written as
    # `allow_private=settings.ALLOW_PRIVATE_FETCH`, the source says nothing and
    # the live object says everything.
    app = Wreath()
    app._http_clients["metadata"] = _Client()
    findings = audit_configuration(app)
    assert [finding.rule_id for finding in findings] == ["ssrf-policy-widened"]
    assert "allow_private, allow_loopback" in findings[0].message


def test_a_default_destination_policy_is_quiet() -> None:
    class _Default:
        _destination = type("P", (), {})()

    app = Wreath()
    app._http_clients["upstream"] = _Default()
    assert audit_configuration(app) == []
