"""`wreath doctor preflight` -- every refusal wreath already knows, asked at once.

The command aggregates rather than invents, so the tests are about the seams:
that each source reaches the report, that severity survives the crossing, and
that what preflight *cannot* see is named rather than omitted. The last is the
one that matters -- a report that lists three findings and stops reads as "there
are three", and the ones needing a database would then never be run at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

from wreath._cli import main
from wreath.app import Wreath
from wreath.config import Env
from wreath.doctor import (
    Preflight,
    preflight,
    render_preflight,
    render_route_manifest,
    route_manifest,
)
from wreath.http_client import DestinationPolicy
from wreath.request import Request

DSN = "postgresql://trek@db.internal:5432/trek"


@dataclass(frozen=True, slots=True)
class Settings:
    token: Annotated[str, Env("TOKEN")]


def build() -> Wreath:
    app = Wreath()

    @app.get("/health")
    async def health(request: Request) -> dict:
        return {"ok": True}

    return app


def test_a_bare_application_produces_a_report_rather_than_an_error() -> None:
    report = preflight(build(), application="tests:app")
    assert isinstance(report, Preflight)
    assert report.application == "tests:app"


def test_a_settings_key_nothing_supplies_is_blocking() -> None:
    """The finding infra already exits 1 for: a deployment that starts and dies."""
    report = preflight(build(), settings=[(Settings, "Settings", "")], supplied={})
    keys = [finding for finding in report.blocking if finding.subject == "TOKEN"]
    assert len(keys) == 1
    assert keys[0].source == "infra"


def test_a_settings_key_that_is_supplied_is_not_a_finding() -> None:
    report = preflight(
        build(), settings=[(Settings, "Settings", "")], supplied={"TOKEN": "process"})
    assert [finding.subject for finding in report.findings if finding.source == "infra"] == []


def test_a_hardening_defect_reaches_the_report_as_blocking() -> None:
    """`hardening` is wreath's own startup ruleset; preflight reports what it
    would refuse without making the process refuse to come up.

    A widened `DestinationPolicy` is the case chosen deliberately: it is read
    off the live object graph, so it is the half of the ruleset that a source
    scan structurally cannot reach and that nothing but a boot would otherwise
    surface.
    """
    app = build()
    app.http_client(
        "meta", base_url="https://example.internal",
        destination=DestinationPolicy(allow_private=True),
    )
    report = preflight(app)
    hardening = [finding for finding in report.findings if finding.source == "hardening"]
    assert [finding.subject for finding in hardening] == ["ssrf-policy-widened"]
    assert hardening[0].severity == "blocking"


def test_the_source_ruleset_is_named_as_unchecked_rather_than_run_twice() -> None:
    """`wreath audit code` owns the source tier and preflight says so.

    Running it here as well would be a second spelling of one gate, and the two
    would drift; naming it keeps the reader pointed at the one that is real.
    """
    assert any("wreath audit code" in entry for entry in preflight(build()).unchecked)


def test_a_route_that_asks_nothing_of_the_caller_is_advisory_not_blocking() -> None:
    """Public routes are a fact about the application, not a defect.

    Reporting them as blocking would mean every health check and every login
    endpoint failed the gate, which is how a gate gets turned off.
    """
    report = preflight(build())
    public = [finding for finding in report.findings if finding.source == "routes"]
    assert public and all(finding.severity == "advisory" for finding in public)
    assert any("/health" in finding.detail for finding in public)


def test_the_route_finding_says_declaration_rather_than_claiming_the_route_is_open() -> None:
    """Found on the camera-trap example, which is why the wording is pinned.

    `crud`'s `Access.deny()` attaches nothing to the requirement and refuses
    inside the handler, so `POST /admin/stations` -- a route that admits *nobody*
    -- has `access_level == 0` and appears in this list. A report that called
    that "open" would be naming the safest route in the application as the risk,
    which is worse than not reporting at all.
    """
    detail = next(
        finding.detail for finding in preflight(build()).findings
        if finding.source == "routes"
    )
    assert "declare no authentication or authorization" in detail
    assert "Access.deny()" in detail


def test_the_report_names_what_it_could_not_check() -> None:
    """Absence of a finding must not read as absence of a problem.

    `wreath doctor trace` already prints a "not searched" section for the same
    reason: a forensic tool that silently omits a source reads as one that
    looked there and found nothing.
    """
    report = preflight(build())
    assert report.unchecked
    joined = " ".join(report.unchecked)
    assert "wreath schema check" in joined
    assert "wreath migrations detect" in joined


def test_the_rendered_report_prints_the_unchecked_section_even_when_clean() -> None:
    rendered = render_preflight(preflight(build()))
    assert "not checked" in rendered


def test_the_command_exits_zero_on_an_application_with_no_blocking_finding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["doctor", "preflight", "tests.preflight_app:app"]) == 0
    assert "not checked" in capsys.readouterr().out


def test_the_command_exits_one_when_something_blocks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blocking finding is a build failure, which is the point of the command."""
    code = main([
        "doctor", "preflight", "tests.preflight_app:app",
        "--settings", "tests.preflight_app:Settings",
    ])
    assert code == 1
    assert "TOKEN" in capsys.readouterr().out


def test_a_bad_settings_spec_is_a_usage_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--settings` resolves a module path, and a wrong one is a typo.

    `wreath infra infer` already turns that into `wreath: error: ...` and exit 2;
    reaching the same helper through a different command must not reach it
    differently, or the same mistake reports two ways depending on which command
    you typed.
    """
    assert main([
        "doctor", "preflight", "tests.preflight_app:app",
        "--settings", "tests.no_such_settings_module:Settings",
    ]) == 2
    assert "could not import settings module" in capsys.readouterr().err


def test_the_json_form_separates_blocking_from_advisory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["doctor", "preflight", "tests.preflight_app:app", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1
    assert set(payload) >= {"application", "findings", "unchecked", "blocking"}
    assert all(
        {"source", "severity", "subject", "detail"} <= set(row) for row in payload["findings"]
    )


def test_the_route_manifest_is_stable_and_names_the_access_decision() -> None:
    from wreath.auth import public

    app = Wreath(require_access_declarations=True)

    @app.get("/health", operation_id="health")
    @public()
    async def health(request: Request) -> dict[str, bool]:
        return {"ok": True}

    manifest = route_manifest(app, application="tests:manifest")
    (route,) = manifest["routes"]
    assert route["operation_id"] == "health"
    assert route["security"]["access"] == "public"
    assert route["security"]["declared"] is True
    assert route["response"] == {
        "kind": "record",
        "arguments": [{"kind": "boolean"}],
    }
    assert render_route_manifest(manifest) == render_route_manifest(
        route_manifest(app, application="tests:manifest")
    )


def test_doctor_routes_writes_and_checks_a_versioned_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "routes.json"
    assert main([
        "doctor", "routes", "tests.preflight_app:app", "--write", str(path),
    ]) == 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["routes"]
    assert main([
        "doctor", "routes", "tests.preflight_app:app", "--check", str(path),
    ]) == 0

    path.write_text("{}\n", encoding="utf-8")
    assert main([
        "doctor", "routes", "tests.preflight_app:app", "--check", str(path),
    ]) == 1
    assert "differs" in capsys.readouterr().err


def test_the_manifest_and_vocabulary_cover_five_hundred_routes() -> None:
    """The large-project contract: no hand-maintained second action list."""
    from enum import StrEnum

    from wreath.auth import BearerTokenBackend
    from wreath.authorization import AuthorizationVocabulary, authorize

    actions_type = StrEnum(
        "Actions", {f"READ_{index}": f"Resource::{index}" for index in range(500)}
    )
    actions = list(actions_type)
    app = Wreath(require_access_declarations=True)
    app.configure_auth(
        BearerTokenBackend({}), vocabulary=AuthorizationVocabulary(actions_type)
    )

    def endpoint_for(index: int):
        @authorize(action=actions[index], resource=f'Resource::"{index}"')
        async def endpoint(request: Request) -> dict[str, int]:
            return {"index": index}

        return endpoint

    for index in range(500):
        app.get(f"/resources/{index}", operation_id=f"readResource{index}")(
            endpoint_for(index)
        )

    manifest = route_manifest(app, application="tests:large")
    assert len(manifest["routes"]) == 500
    assert len(manifest["authorization"]["declared"]) == 500
    assert manifest["authorization"]["unknown"] == []
    assert manifest["authorization"]["unused"] == []
