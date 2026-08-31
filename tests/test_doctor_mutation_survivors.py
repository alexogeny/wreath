from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wreath._dns import DnsAnswer
from wreath.doctor import (
    _access,
    _public_routes,
    check_email_deliverability,
    preflight,
    route_manifest,
)


def _resolve(records: dict[str, tuple[str, ...]]):
    def resolve(name: str, *, timeout: float) -> DnsAnswer:
        assert timeout == 0.25
        return DnsAnswer(name, records.get(name, ()))

    return resolve


def _sender(*, from_addr: str | None = "mail@example.com", dkim: Any = None):
    return SimpleNamespace(from_addr=from_addr, dkim=dkim)


def _signer(*, algorithm: str = "rsa-sha256"):
    return SimpleNamespace(
        domain="example.com",
        selector="mail",
        algorithm=algorithm,
    )


def _healthy_records() -> dict[str, tuple[str, ...]]:
    return {
        "mail._domainkey.example.com": ("v=DKIM1; k=rsa; p=KEY",),
        "example.com": ("v=spf1 -all",),
        "_dmarc.example.com": ("v=DMARC1; p=reject",),
    }


def test_email_check_uses_the_supplied_resolver_and_timeout() -> None:
    assert (
        check_email_deliverability(
            _sender(dkim=_signer()),
            timeout=0.25,
            resolve=_resolve(_healthy_records()),
        )
        == []
    )


def test_email_check_uses_the_default_resolver_when_none_is_supplied(monkeypatch) -> None:
    calls: list[str] = []
    records = _healthy_records()

    def resolve(name: str, *, timeout: float) -> DnsAnswer:
        calls.append(name)
        return DnsAnswer(name, records.get(name, ()))

    monkeypatch.setattr("wreath._dns.resolve_txt", resolve)
    assert check_email_deliverability(_sender(dkim=_signer()), timeout=0.25) == []
    assert calls == [
        "mail._domainkey.example.com",
        "example.com",
        "_dmarc.example.com",
    ]


def test_email_check_normalises_a_none_from_address_before_parsing() -> None:
    findings = check_email_deliverability(
        _sender(from_addr=None),
        timeout=0.25,
        resolve=_resolve({}),
    )
    assert len(findings) == 1
    assert "no from address" in findings[0]


def test_dkim_accepts_a_key_tag_without_a_version_tag() -> None:
    records = _healthy_records()
    records["mail._domainkey.example.com"] = ("p=KEY",)
    assert (
        check_email_deliverability(
            _sender(dkim=_signer()),
            timeout=0.25,
            resolve=_resolve(records),
        )
        == []
    )


def test_unrelated_dkim_txt_records_do_not_count_as_a_public_key() -> None:
    records = _healthy_records()
    records["mail._domainkey.example.com"] = ("google-site-verification=token",)
    findings = check_email_deliverability(
        _sender(dkim=_signer()),
        timeout=0.25,
        resolve=_resolve(records),
    )
    assert any("no DKIM public key" in finding for finding in findings)


def test_rsa_dkim_does_not_require_an_ed25519_key_tag() -> None:
    records = _healthy_records()
    records["mail._domainkey.example.com"] = ("v=DKIM1; p=KEY",)
    assert (
        check_email_deliverability(
            _sender(dkim=_signer()),
            timeout=0.25,
            resolve=_resolve(records),
        )
        == []
    )


def test_ed25519_dkim_accepts_its_declared_key_tag() -> None:
    records = _healthy_records()
    records["mail._domainkey.example.com"] = ("v=DKIM1; k=ed25519; p=KEY",)
    assert (
        check_email_deliverability(
            _sender(dkim=_signer(algorithm="ed25519-sha256")),
            timeout=0.25,
            resolve=_resolve(records),
        )
        == []
    )


@pytest.mark.parametrize(
    ("name", "record", "message"),
    [
        ("example.com", "site-verification=token", "no SPF record"),
        ("_dmarc.example.com", "site-verification=token", "no DMARC record"),
    ],
)
def test_unrelated_txt_records_do_not_satisfy_mail_policies(
    name: str,
    record: str,
    message: str,
) -> None:
    records = _healthy_records()
    records[name] = (record,)
    findings = check_email_deliverability(
        _sender(dkim=_signer()),
        timeout=0.25,
        resolve=_resolve(records),
    )
    assert any(message in finding for finding in findings)


def _requirement(**overrides: Any):
    values = {
        "public": False,
        "identify": False,
        "policies": (),
        "access_level": 0,
        "declares_access": False,
        "role_checks": (),
        "permission_checks": (),
        "second_factor": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (_requirement(public=True), "public"),
        (_requirement(identify=True), "identified-public"),
        (_requirement(policies=(object(),)), "authorized"),
        (_requirement(access_level=2), "administrator"),
        (_requirement(access_level=1), "authenticated"),
        (_requirement(), "implicit-public"),
    ],
)
def test_access_names_every_requirement_tier(requirement: Any, expected: str) -> None:
    assert _access(requirement) == expected


def test_public_routes_refuses_an_uncompiled_image() -> None:
    assert _public_routes(SimpleNamespace(_application_image=None)) == []


def test_public_routes_selects_only_level_zero_and_sorts_methods() -> None:
    routes = (
        SimpleNamespace(methods={"POST", "GET"}, path="/open"),
        SimpleNamespace(methods={"GET"}, path="/closed"),
        SimpleNamespace(methods=set(), path="/any"),
    )
    image = SimpleNamespace(
        routes=lambda: routes,
        requirements=lambda: (
            _requirement(access_level=0),
            _requirement(access_level=1),
            _requirement(access_level=0),
        ),
    )
    assert _public_routes(SimpleNamespace(_application_image=image)) == [
        "GET/POST /open",
        "ANY /any",
    ]


def test_preflight_preserves_explicit_and_default_infer_arguments(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []

    def infer(app: Any, **kwargs: Any):
        captured.append(kwargs)
        return SimpleNamespace(gaps=())

    monkeypatch.setattr("wreath.infra.infer", infer)
    monkeypatch.setattr("wreath.hardening.audit_configuration", lambda app: ())
    monkeypatch.setattr("wreath.doctor._tenancy_findings", lambda app: [])
    monkeypatch.setattr("wreath.doctor._public_routes", lambda app: [])

    default = preflight(object(), supplied=None, dotenv_keys=None)
    named = preflight(
        object(),
        application="shop.app:app",
        supplied={"TOKEN": "process"},
        dotenv_keys={"OTHER": ".env"},
    )

    assert default.application == "application"
    assert captured[0]["application"] == "application"
    assert captured[0]["supplied"] == {}
    assert captured[0]["dotenv_keys"] == {}
    assert named.application == "shop.app:app"
    assert captured[1]["application"] == "shop.app:app"
    assert captured[1]["supplied"] == {"TOKEN": "process"}
    assert captured[1]["dotenv_keys"] == {"OTHER": ".env"}


def test_preflight_classifies_both_infra_and_hardening_severities(monkeypatch) -> None:
    gaps = (
        SimpleNamespace(kind="settings-key", subject="TOKEN", detail="missing"),
        SimpleNamespace(kind="advisory", subject="CACHE", detail="inspect"),
    )
    hardening = (
        SimpleNamespace(
            severity=SimpleNamespace(value="error"),
            rule_id="strict",
            surface="app",
            message="blocked",
        ),
        SimpleNamespace(
            severity=SimpleNamespace(value="warning"),
            rule_id="review",
            surface="app",
            message="review it",
        ),
    )
    monkeypatch.setattr("wreath.infra.infer", lambda *args, **kwargs: SimpleNamespace(gaps=gaps))
    monkeypatch.setattr("wreath.hardening.audit_configuration", lambda app: hardening)
    monkeypatch.setattr("wreath.doctor._tenancy_findings", lambda app: [])
    monkeypatch.setattr("wreath.doctor._public_routes", lambda app: [])

    report = preflight(object())
    assert [(finding.subject, finding.severity) for finding in report.findings] == [
        ("TOKEN", "blocking"),
        ("CACHE", "advisory"),
        ("strict", "blocking"),
        ("review", "advisory"),
    ]


@pytest.mark.parametrize(
    ("routes", "expected"),
    [
        ([], None),
        ([f"GET /route-{index}" for index in range(12)], "GET /route-11"),
        ([f"GET /route-{index}" for index in range(13)], "and 1 more"),
    ],
)
def test_preflight_route_summary_handles_empty_sampled_and_overflow_lists(
    monkeypatch,
    routes: list[str],
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        "wreath.infra.infer",
        lambda *args, **kwargs: SimpleNamespace(gaps=()),
    )
    monkeypatch.setattr("wreath.hardening.audit_configuration", lambda app: ())
    monkeypatch.setattr("wreath.doctor._tenancy_findings", lambda app: [])
    monkeypatch.setattr("wreath.doctor._public_routes", lambda app: routes)

    route_findings = [
        finding for finding in preflight(object()).findings if finding.source == "routes"
    ]
    if expected is None:
        assert route_findings == []
    else:
        assert len(route_findings) == 1
        assert expected in route_findings[0].detail
        assert ("more" in route_findings[0].detail) == (len(routes) > 12)


def test_route_manifest_handles_an_app_without_a_compiled_image(monkeypatch) -> None:
    built: list[Any] = []
    app = SimpleNamespace(_application_image=None, _ws_routes=())
    monkeypatch.setattr(
        "wreath.typegen.inspect.build_api_model",
        lambda candidate, **kwargs: built.append(candidate) or SimpleNamespace(operations=()),
    )
    monkeypatch.setattr("wreath._auth.permissions.declared_actions", lambda candidate: {})

    manifest = route_manifest(app)
    assert built == [app]
    assert manifest["application"] == "application"
    assert manifest["routes"] == []
    assert manifest["authorization"] == {
        "declared": [],
        "vocabulary": None,
        "unknown": [],
        "unused": [],
    }


def test_route_manifest_calls_a_callable_compiler_and_refuses_diagnostics(monkeypatch) -> None:
    from wreath.typegen.model import TypegenError

    compiled: list[bool] = []
    diagnostic = SimpleNamespace(render=lambda: "duplicate operation id")
    image = SimpleNamespace(
        routes=lambda: (),
        requirements=lambda: (),
        operation_ids=lambda: ({}, (diagnostic,)),
    )
    app = SimpleNamespace(
        _compile_routes=lambda: compiled.append(True),
        _application_image=image,
    )

    with pytest.raises(TypegenError, match="duplicate operation id"):
        route_manifest(app)
    assert compiled == [True]


def test_route_manifest_keeps_a_route_without_a_typegen_operation(monkeypatch) -> None:
    async def endpoint() -> None:
        return None

    route = SimpleNamespace(
        methods={"GET"},
        path="/raw",
        name="raw",
        endpoint=endpoint,
        tags=(),
        dependencies=(),
        middleware=(),
    )
    image = SimpleNamespace(
        routes=lambda: (route,),
        requirements=lambda: (_requirement(),),
        operation_ids=lambda: ({(0, "GET"): "raw"}, ()),
    )
    app = SimpleNamespace(_application_image=image, _ws_routes=())
    monkeypatch.setattr(
        "wreath.typegen.inspect.build_api_model",
        lambda candidate, **kwargs: SimpleNamespace(operations=()),
    )
    monkeypatch.setattr("wreath._auth.permissions.declared_actions", lambda candidate: {})

    manifest = route_manifest(app, application="raw-app")
    assert manifest["application"] == "raw-app"
    (entry,) = manifest["routes"]
    assert entry["operation_id"] == "raw"
    assert entry["request"] is None
    assert entry["response"] is None
    assert entry["security"]["access"] == "implicit-public"


def test_route_manifest_serializes_a_request_body(monkeypatch) -> None:
    async def endpoint() -> None:
        return None

    route = SimpleNamespace(
        methods={"POST"},
        path="/items",
        name="create-item",
        endpoint=endpoint,
        tags=(),
        dependencies=(),
        middleware=(),
    )
    image = SimpleNamespace(
        routes=lambda: (route,),
        requirements=lambda: (_requirement(),),
        operation_ids=lambda: ({(0, "POST"): "createItem"}, ()),
    )
    type_ref = SimpleNamespace(kind="string", name=None, arguments=(), literals=())
    operation = SimpleNamespace(
        method="POST",
        path="/items",
        parameters=(),
        request_body=type_ref,
        request_body_media_type="application/json",
        response_body=type_ref,
    )
    app = SimpleNamespace(_application_image=image, _ws_routes=())
    monkeypatch.setattr(
        "wreath.typegen.inspect.build_api_model",
        lambda candidate, **kwargs: SimpleNamespace(operations=(operation,)),
    )
    monkeypatch.setattr("wreath._auth.permissions.declared_actions", lambda candidate: {})

    (entry,) = route_manifest(app)["routes"]
    assert entry["request"]["body"] == {"kind": "string"}
    assert entry["request"]["body_media_type"] == "application/json"


def test_route_manifest_serializes_an_authorization_vocabulary(monkeypatch) -> None:
    vocabulary = SimpleNamespace(
        actions=("read", "write"),
        unknown=lambda declared: ("unknown",),
        unused=lambda declared: ("write",),
    )
    app = SimpleNamespace(
        _application_image=None,
        _ws_routes=(),
        _authorization_vocabulary=vocabulary,
    )
    monkeypatch.setattr(
        "wreath.typegen.inspect.build_api_model",
        lambda candidate, **kwargs: SimpleNamespace(operations=()),
    )
    monkeypatch.setattr(
        "wreath._auth.permissions.declared_actions",
        lambda candidate: {object(): ("read", "unknown")},
    )

    manifest = route_manifest(app, application="shop.app:app")
    assert manifest["application"] == "shop.app:app"
    assert manifest["authorization"] == {
        "declared": ["read", "unknown"],
        "vocabulary": ["read", "write"],
        "unknown": ["unknown"],
        "unused": ["write"],
    }
