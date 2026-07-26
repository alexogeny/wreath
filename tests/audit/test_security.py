"""Runtime security/compliance audit rules — the downstream-SaaS checks."""
from __future__ import annotations

from wreath._audit.model import Report, Severity
from wreath._audit.rules import RESPONSE_SECURITY_RULES, ResponseView
from wreath._audit.runtime import audit_response


def _fire(view: ResponseView) -> dict[str, Severity]:
    findings: dict[str, Severity] = {}
    for rule in RESPONSE_SECURITY_RULES:
        for f in rule(view):
            findings[f.rule_id] = f.severity
    return findings


def _view(status=200, scheme="http", headers=None, cookies=()):
    return ResponseView(status=status, scheme=scheme, surface="runtime:/",
                        headers={k.lower(): v for k, v in (headers or {}).items()},
                        set_cookies=tuple(cookies))


def test_samesite_none_without_secure_is_an_error() -> None:
    fired = _fire(_view(cookies=["sid=abc; SameSite=None"]))
    assert fired["cookie-samesite-none-insecure"] is Severity.ERROR


def test_host_prefix_violation_is_an_error() -> None:
    # __Host- forbids Domain; this one sets it, so it is invalid.
    bad = ["__Host-sid=x; Secure; Domain=example.com; Path=/"]
    fired = _fire(_view(scheme="https", cookies=bad))
    assert fired["cookie-prefix"] is Severity.ERROR


def test_well_formed_cookie_only_warns_on_missing_defenses() -> None:
    fired = _fire(_view(scheme="https", cookies=["sid=x; Secure; HttpOnly; SameSite=Lax"]))
    assert "cookie-samesite-none-insecure" not in fired
    assert "cookie-prefix" not in fired
    assert "cookie-secure" not in fired and "cookie-httponly" not in fired
    assert "cookie-samesite" not in fired


def test_missing_cookie_defenses_warn() -> None:
    fired = _fire(_view(scheme="https", cookies=["sid=x"]))
    assert fired["cookie-secure"] is Severity.WARN
    assert fired["cookie-httponly"] is Severity.WARN
    assert fired["cookie-samesite"] is Severity.WARN


def test_hsts_required_on_https_only() -> None:
    assert "hsts" in _fire(_view(scheme="https"))
    assert "hsts" not in _fire(_view(scheme="http"))
    ok = _fire(_view(scheme="https",
                     headers={"Strict-Transport-Security": "max-age=63072000"}))
    assert "hsts" not in ok and "hsts-max-age" not in ok


def test_401_requires_www_authenticate() -> None:
    assert _fire(_view(status=401))["www-authenticate"] is Severity.ERROR
    assert "www-authenticate" not in _fire(
        _view(status=401, headers={"WWW-Authenticate": "Bearer"}))


def test_405_requires_allow() -> None:
    assert _fire(_view(status=405))["allow-header"] is Severity.ERROR
    assert "allow-header" not in _fire(_view(status=405, headers={"Allow": "GET, POST"}))


def test_cors_wildcard_with_credentials_is_an_error() -> None:
    fired = _fire(_view(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Credentials": "true",
    }))
    assert fired["cors-credentials"] is Severity.ERROR


def test_nosniff_and_referrer_policy_reported_when_absent() -> None:
    fired = _fire(_view())
    assert fired["content-type-options"] is Severity.WARN
    assert fired["referrer-policy"] is Severity.INFO


def test_runtime_audit_response_runs_security_rules() -> None:
    # End-to-end through the runtime entrypoint: a 401 with an insecure cookie.
    report = Report()
    audit_response(
        401, {"content-type": "application/json"}, "", "runtime:/login", report,
        scheme="https", set_cookies=["sid=x; SameSite=None"],
    )
    ids = {f.rule_id for f in report.findings}
    assert {"www-authenticate", "cookie-samesite-none-insecure", "hsts"} <= ids
