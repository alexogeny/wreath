from __future__ import annotations

from typing import Any

import pytest

from tests.admin._doubles import FakeSession, Request, routes
from wreath._audit import Report, audit_response
from wreath.admin import CONTENT_SECURITY_POLICY, Admin
from wreath.crud import Access

pytestmark = pytest.mark.asyncio


def _handlers(model: type, session: FakeSession) -> dict:
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(model)
    return routes(admin.router())


def _rows(model: type, count: int = 3) -> dict:
    return {
        n: model(
            id=n,
            name=f"Person {n}",
            email=f"p{n}@example.test",
            note="a note" if n % 2 else None,
            active=bool(n % 2),
        )
        for n in range(1, count + 1)
    }


async def _pages(model: type) -> dict[str, Any]:
    """Every page the admin can draw, rendered for real."""
    session = FakeSession(_rows(model))
    handlers = _handlers(model, session)
    detail = Request(path_params={"pk": "1"})
    return {
        "index": await handlers[("GET", "/admin")](Request()),
        "list": await handlers[("GET", "/admin/account/")](Request()),
        "detail": await handlers[("GET", "/admin/account/{pk}")](detail),
        "create": await handlers[("GET", "/admin/account/new")](Request()),
        "edit": await handlers[("GET", "/admin/account/{pk}/edit")](
            Request(path_params={"pk": "1"})
        ),
        "delete": await handlers[("GET", "/admin/account/{pk}/delete")](
            Request(path_params={"pk": "1"})
        ),
        "missing": await handlers[("GET", "/admin/account/{pk}")](
            Request(path_params={"pk": "404"})
        ),
    }


def _audit(name: str, response: Any) -> Report:
    report = Report()
    headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in response.headers}
    audit_response(
        response.status,
        headers,
        response.body.decode("utf-8"),
        f"admin:{name}",
        report,
        scheme="https",
    )
    return report


async def test_the_auditor_can_fail(account_model: type) -> None:
    report = Report()
    audit_response(
        200,
        {"content-type": "text/html"},
        "<html><body><input type='text' name='x'></body></html>",
        "probe",
        report,
    )
    rules = {finding.rule_id for finding in report.errors}
    assert {"html-lang", "document-title", "control-label"} <= rules


@pytest.mark.parametrize(
    "page",
    ["index", "list", "detail", "create", "edit", "delete", "missing"],
)
async def test_every_admin_page_has_no_accessibility_errors(account_model: type, page: str) -> None:
    rendered = await _pages(account_model)
    report = _audit(page, rendered[page])
    assert report.errors == [], [f.as_dict() for f in report.errors]


@pytest.mark.parametrize(
    "page",
    ["index", "list", "detail", "create", "edit", "delete", "missing"],
)
async def test_every_admin_page_has_no_accessibility_warnings(
    account_model: type, page: str
) -> None:
    rendered = await _pages(account_model)
    report = _audit(page, rendered[page])
    # Deployment-level rules, not page-level ones. Each names a middleware the
    # application mounts for every route; an admin router that set them would be
    # deciding compression, caching and transport policy for the whole origin.
    # `x-content-type-options` is deliberately *not* here: that one is about this
    # response, so the admin sets it and the rule stays live.
    deployment = {"compression-enabled", "cache-control", "security-headers", "hsts"}
    warnings = [f for f in report.warnings if f.rule_id not in deployment]
    assert warnings == [], [f.as_dict() for f in warnings]


async def test_every_page_carries_the_scriptless_policy(account_model: type) -> None:
    for name, response in (await _pages(account_model)).items():
        headers = {key.decode(): value.decode() for key, value in response.headers}
        assert headers.get("content-security-policy") == CONTENT_SECURITY_POLICY, name
        assert "script-src" not in CONTENT_SECURITY_POLICY
        assert "default-src 'none'" in CONTENT_SECURITY_POLICY


async def test_no_page_contains_a_script_element(account_model: type) -> None:
    for name, response in (await _pages(account_model)).items():
        body = response.body.decode("utf-8").lower()
        assert "<script" not in body, name
        assert "onclick" not in body, name


async def test_a_row_value_is_escaped_into_the_page(account_model: type) -> None:
    session = FakeSession(
        {
            1: account_model(
                id=1,
                name="<script>alert(1)</script>",
                email="e@x.test",
                note=None,
                active=True,
            )
        }
    )
    handlers = _handlers(account_model, session)
    response = await handlers[("GET", "/admin/account/{pk}")](Request(path_params={"pk": "1"}))

    body = response.body.decode("utf-8")
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
