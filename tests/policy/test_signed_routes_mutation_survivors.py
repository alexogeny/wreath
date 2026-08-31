from __future__ import annotations

from typing import Any

import pytest

from wreath.policy import SignedRoutePolicy
from wreath.request import Request
from wreath.response import ProblemResponse
from wreath.tokens import ActionTokens, TokenPurpose


def _tokens() -> ActionTokens:
    return ActionTokens(
        {"current": b"s" * 32},
        current="current",
        purposes=(TokenPurpose("download", 60),),
        clock=lambda: 1_000.0,
    )


def _policy(**options: Any) -> SignedRoutePolicy:
    return SignedRoutePolicy(
        _tokens(),
        "download",
        ("/download",),
        **options,
    )


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(*, method: str = "GET", path: str = "/download", query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query,
            "headers": [],
        },
        _receive,
    )


@pytest.mark.parametrize("path", [7, "/download?file=x", "/download#fragment"])
def test_each_invalid_protected_path_shape_is_refused(path: Any) -> None:
    with pytest.raises(ValueError, match="exact absolute paths"):
        SignedRoutePolicy(_tokens(), "download", (path,))


def test_non_string_method_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="valid HTTP methods"):
        _policy(methods=(7,))


def test_empty_method_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="valid HTTP methods"):
        _policy(methods=())


def test_malformed_method_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="valid HTTP methods"):
        _policy(methods=("BAD METHOD",))


@pytest.mark.parametrize("parameter", [7, "bad parameter"])
def test_each_invalid_signature_parameter_is_refused(parameter: Any) -> None:
    with pytest.raises(ValueError, match="valid HTTP token"):
        _policy(parameter=parameter)


@pytest.mark.parametrize("detail", [7, ""])
def test_each_invalid_refusal_detail_is_refused(detail: Any) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _policy(detail=detail)


@pytest.mark.parametrize(
    "path",
    ["/other", "/download?x#fragment", "/download?a=1?b=2"],
)
def test_sign_refuses_each_ambiguous_path_shape(path: str) -> None:
    with pytest.raises(ValueError, match="exact protected paths"):
        _policy().sign(path)


def test_sign_refuses_a_method_outside_the_declared_set() -> None:
    with pytest.raises(ValueError, match="method is not protected"):
        _policy().sign("/download", method="POST")


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/download"), ("GET", "/public")],
)
def test_ingress_ignores_requests_outside_either_protected_dimension(
    method: str, path: str
) -> None:
    assert _policy()._ingress_sync(_request(method=method, path=path)) is None


def test_ingress_refuses_duplicate_signature_parameters() -> None:
    policy = _policy()
    result = policy._ingress_sync(_request(query=b"signature=one&signature=two"))

    assert isinstance(result, ProblemResponse)
    assert result.status == 403


def test_ingress_refuses_a_duplicate_even_when_the_last_signature_is_valid() -> None:
    policy = _policy()
    signed = policy.sign("/download")
    valid_signature = signed.partition("signature=")[2]
    query = f"signature=attacker&signature={valid_signature}".encode()

    result = policy._ingress_sync(_request(query=query))

    assert isinstance(result, ProblemResponse)
    assert result.status == 403


def test_ingress_refuses_a_missing_signature_parameter() -> None:
    result = _policy()._ingress_sync(_request(query=b"file=report.pdf"))

    assert isinstance(result, ProblemResponse)
    assert result.status == 403


def test_ingress_ignores_empty_query_segments_when_rebuilding_the_target() -> None:
    policy = _policy()
    signed = policy.sign("/download?file=report.pdf")
    query = signed.partition("?")[2].replace("&signature=", "&&signature=").encode()

    assert policy._ingress_sync(_request(query=query)) is None


def test_ingress_refuses_claims_for_another_subject() -> None:
    policy = _policy()
    target = "/download?file=report.pdf"
    token = policy._tokens.issue(
        "download",
        "/download?file=other.pdf",
        bound="GET\x00" + target,
    )
    query = f"file=report.pdf&signature={token}".encode()

    result = policy._ingress_sync(_request(query=query))

    assert isinstance(result, ProblemResponse)
    assert result.status == 403
