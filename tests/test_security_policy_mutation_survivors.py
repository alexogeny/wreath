from __future__ import annotations

import pytest

from wreath.policy.security import SecurityHeadersPolicy


def test_security_headers_can_disable_every_optional_header() -> None:
    policy = SecurityHeadersPolicy(
        content_security_policy=None,
        frame_options=None,
        content_type_options=False,
        referrer_policy=None,
        permissions_policy=None,
    )

    assert policy.headers == ()
    assert policy.hsts_header is None
    assert policy.https_headers == ()
    assert not policy._has_nonce


def test_security_headers_compile_every_configured_header_once() -> None:
    policy = SecurityHeadersPolicy(
        content_security_policy="default-src 'none'",
        csp_report_only=True,
        frame_options="SAMEORIGIN",
        content_type_options=True,
        referrer_policy="no-referrer",
        permissions_policy="camera=()",
        hsts_max_age=31_536_000,
        hsts_include_subdomains=True,
        hsts_preload=True,
    )

    assert policy.headers == (
        (b"content-security-policy-report-only", b"default-src 'none'"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=()"),
    )
    assert policy.hsts_header == (
        b"strict-transport-security",
        b"max-age=31536000; includeSubDomains; preload",
    )
    assert policy.https_headers == (*policy.headers, policy.hsts_header)


def test_raw_hsts_is_kept_without_structured_directives() -> None:
    policy = SecurityHeadersPolicy(strict_transport_security="max-age=10")

    assert policy.hsts_header == (b"strict-transport-security", b"max-age=10")


@pytest.mark.parametrize("directive", [None, "", "script src"])
def test_nonce_directive_must_be_a_nonempty_http_token(directive: object) -> None:
    with pytest.raises(ValueError, match="non-empty directive names"):
        SecurityHeadersPolicy(csp_nonce_directives=(directive,))


def test_nonce_directives_require_a_csp_value() -> None:
    with pytest.raises(ValueError, match="require content_security_policy"):
        SecurityHeadersPolicy(content_security_policy=None, csp_nonce_directives=("script-src",))


def test_report_only_flag_must_be_a_real_boolean() -> None:
    with pytest.raises(ValueError, match="must be a bool"):
        SecurityHeadersPolicy(csp_report_only=1)


@pytest.mark.parametrize("line_break", ["\r", "\n"])
def test_csp_refuses_each_header_line_break(line_break: str) -> None:
    with pytest.raises(ValueError, match="must not contain a line break"):
        SecurityHeadersPolicy(content_security_policy=f"default-src 'self'{line_break}script-src")


def test_nonce_template_drops_blank_segments_and_adds_missing_directives() -> None:
    policy = SecurityHeadersPolicy(
        content_security_policy="default-src 'self'; ; script-src 'self';",
        csp_nonce_directives=("script-src", "style-src"),
    )

    assert policy.headers == (
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
    )
    assert policy._csp_template == (
        b"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
        b"style-src 'nonce-{nonce}'"
    )
    assert policy._has_nonce


@pytest.mark.parametrize(
    "settings",
    [
        {"hsts_max_age": 31_536_000, "hsts_preload": True},
        {"hsts_include_subdomains": True, "hsts_preload": True},
        {
            "hsts_max_age": 31_535_999,
            "hsts_include_subdomains": True,
            "hsts_preload": True,
        },
    ],
)
def test_hsts_preload_refuses_each_missing_prerequisite(settings: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="HSTS preload requires"):
        SecurityHeadersPolicy(**settings)


def test_describe_distinguishes_static_and_nonce_csp_headers() -> None:
    static = SecurityHeadersPolicy().describe().response_headers
    nonce = SecurityHeadersPolicy(csp_nonce_directives=("script-src",)).describe().response_headers

    static_csp = [spec for _owner, spec in static if spec.name == "Content-Security-Policy"]
    nonce_csp = [spec for _owner, spec in nonce if spec.name == "Content-Security-Policy"]
    assert [spec.const for spec in static_csp] == ["default-src 'self'"]
    assert len(nonce_csp) == 1
    assert nonce_csp[0].const is None
    assert "fresh nonce" in nonce_csp[0].description
