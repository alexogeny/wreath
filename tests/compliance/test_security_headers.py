from __future__ import annotations

from wreath._audit.rules import RESPONSE_SECURITY_RULES, ResponseView


def _fired(view: ResponseView) -> dict[str, str]:
    out: dict[str, str] = {}
    for rule in RESPONSE_SECURITY_RULES:
        for f in rule(view):
            out[f.rule_id] = f.reference
    return out


def _view(status=200, scheme="https", headers=None, cookies=()):
    return ResponseView(
        status=status,
        scheme=scheme,
        surface="compliance",
        headers={k.lower(): v for k, v in (headers or {}).items()},
        set_cookies=tuple(cookies),
    )


def test_rfc6265bis_samesite_none_and_prefixes() -> None:
    assert "cookie-samesite-none-insecure" in _fired(_view(cookies=["s=x; SameSite=None"]))
    assert "cookie-prefix" in _fired(_view(cookies=["__Host-s=x; Secure; Domain=e.com; Path=/"]))
    assert "cookie-prefix" in _fired(_view(cookies=["__Secure-s=x"]))


def test_rfc9110_status_required_headers() -> None:
    assert "www-authenticate" in _fired(_view(status=401))  # §15.5.2
    assert "allow-header" in _fired(_view(status=405))  # §15.5.6
    assert "www-authenticate" not in _fired(
        _view(status=401, headers={"WWW-Authenticate": "Bearer"})
    )


def test_rfc6797_hsts_on_https() -> None:
    assert "hsts" in _fired(_view(scheme="https"))
    assert "hsts" not in _fired(_view(scheme="http"))
    assert "hsts" not in _fired(
        _view(scheme="https", headers={"Strict-Transport-Security": "max-age=63072000"})
    )


def test_fetch_cors_wildcard_with_credentials() -> None:
    assert "cors-credentials" in _fired(
        _view(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Credentials": "true",
            }
        )
    )


def test_owasp_secure_headers_reported_when_absent() -> None:
    fired = _fired(_view())
    assert "content-type-options" in fired
    assert "referrer-policy" in fired


def test_a_hardened_response_is_clean() -> None:
    view = _view(
        scheme="https",
        headers={
            "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
        cookies=["sid=x; Secure; HttpOnly; SameSite=Lax"],
    )
    assert _fired(view) == {}
