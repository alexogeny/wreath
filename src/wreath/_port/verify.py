"""Behavioural comparison for a source application and its emitted Wreath twin.

The static analyzer answers whether a construct has a mechanical translation.
This module asks the separate question that matters after emission: whether two
running ASGI applications give the same HTTP answers to a declared corpus.

Importing this module stays dependency-free and does not import either target.
`verify_apps` imports Wreath's generic ASGI test client lazily, after the
caller has deliberately supplied two application objects.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .._http import _is_http_token
from .._native import _core


@dataclass(frozen=True, slots=True)
class RequestCase:
    """One byte-exact HTTP request sent to both applications."""

    name: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("port verification case name must be non-empty")
        if not _is_http_token(self.method):
            raise ValueError(
                f"port verification case {self.name!r} has invalid HTTP method "
                f"{self.method!r}; use an ASCII token such as GET or POST"
            )
        if not self.path.startswith("/"):
            raise ValueError(
                f"port verification case {self.name!r} path must start with '/': "
                f"{self.path!r}"
            )
        seen_headers: set[str] = set()
        for header_name, header_value in self.headers:
            normalized = header_name.lower()
            if not _is_http_token(header_name):
                raise ValueError(
                    f"port verification case {self.name!r} header name "
                    f"{header_name!r} must be an ASCII HTTP token such as content-type"
                )
            if normalized in seen_headers:
                raise ValueError(
                    f"port verification case {self.name!r} repeats request header "
                    f"{header_name!r}; express each request header once"
                )
            seen_headers.add(normalized)
            try:
                header_value.encode("latin-1")
            except UnicodeEncodeError as error:
                raise ValueError(
                    f"port verification case {self.name!r} header {header_name!r} "
                    "must be latin-1 encodable"
                ) from error


@dataclass(frozen=True, slots=True)
class ResponseSnapshot:
    """The observable HTTP response contract for one application."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "headers": [list(header) for header in self.headers],
            "body_base64": base64.b64encode(self.body).decode("ascii"),
        }


@dataclass(frozen=True, slots=True)
class Difference:
    """One response field on which the source and candidate disagree."""

    case: str
    field: str
    source: object
    candidate: object

    def as_dict(self) -> dict[str, object]:
        return {
            "case": self.case,
            "field": self.field,
            "source": self.source,
            "candidate": self.candidate,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The complete result of driving one declared request corpus."""

    cases: int
    differences: tuple[Difference, ...]

    @property
    def equivalent(self) -> bool:
        return not self.differences

    def as_dict(self) -> dict[str, object]:
        return {
            "cases": self.cases,
            "equivalent": self.equivalent,
            "differences": [difference.as_dict() for difference in self.differences],
        }

    def render_text(self) -> str:
        if self.equivalent:
            return f"{self.cases} case(s) equivalent.\n"
        lines = [
            f"{len(self.differences)} difference(s) across {self.cases} case(s).",
            "",
        ]
        for difference in self.differences:
            lines.append(f"{difference.case}: {difference.field}")
            lines.append(f"  source     {difference.source!r}")
            lines.append(f"  candidate  {difference.candidate!r}")
        return "\n".join(lines) + "\n"


def _headers(
    raw: list[tuple[bytes, bytes]], ignored: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    grouped: dict[str, list[str]] = {}
    for raw_name, raw_value in raw:
        name = raw_name.decode("latin-1").lower()
        if name not in ignored:
            grouped.setdefault(name, []).append(raw_value.decode("latin-1"))
    return tuple(
        (name, value)
        for name in sorted(grouped)
        for value in grouped[name]
    )


def _snapshot(response: Any, ignored: frozenset[str]) -> ResponseSnapshot:
    return ResponseSnapshot(
        status=response.status,
        headers=_headers(response.headers, ignored),
        body=response.body,
    )


def _differences(
    case: RequestCase,
    source: ResponseSnapshot,
    candidate: ResponseSnapshot,
) -> list[Difference]:
    differences: list[Difference] = []
    if source.status != candidate.status:
        differences.append(Difference(case.name, "status", source.status, candidate.status))
    if source.headers != candidate.headers:
        differences.append(
            Difference(case.name, "headers", source.headers, candidate.headers)
        )
    if source.body != candidate.body:
        differences.append(
            Difference(
                case.name,
                "body_base64",
                base64.b64encode(source.body).decode("ascii"),
                base64.b64encode(candidate.body).decode("ascii"),
            )
        )
    return differences


async def verify_apps(
    source_app: Any,
    candidate_app: Any,
    cases: tuple[RequestCase, ...],
    *,
    ignore_headers: tuple[str, ...] = ("date", "server"),
) -> VerificationReport:
    """Drive two ASGI applications through lifespan and compare their replies.

    Header-name order and casing are not HTTP semantics, so names are normalized
    and sorted before comparison. Repeated values remain separate and in wire
    order, because their order can be meaningful. Only `Date` and `Server` are ignored by default;
    callers must opt out of comparing any other header by name.

    The two targets must begin from isolated, equivalent external state. This
    function observes ASGI responses; it does not infer that a database write
    or an outbound request was equivalent merely because the replies matched.
    """
    if not cases:
        raise ValueError("port verification needs at least one request case")
    names = [case.name for case in cases]
    duplicate = _core.first_duplicate(names)
    if duplicate is not None:
        raise ValueError(f"duplicate port verification case name: {duplicate!r}")
    ignored = frozenset(name.lower() for name in ignore_headers)
    for name in ignored:
        if not _is_http_token(name):
            raise ValueError(
                f"ignored response header {name!r} must be an ASCII HTTP token"
            )

    from ..testing import TestClient

    differences: list[Difference] = []
    async with TestClient(source_app) as source, TestClient(candidate_app) as candidate:
        for case in cases:
            headers = dict(case.headers)
            source_response = await source.request(
                case.method, case.path, headers=headers, content=case.body
            )
            candidate_response = await candidate.request(
                case.method, case.path, headers=headers, content=case.body
            )
            differences.extend(
                _differences(
                    case,
                    _snapshot(source_response, ignored),
                    _snapshot(candidate_response, ignored),
                )
            )
    return VerificationReport(len(cases), tuple(differences))


def load_cases(path: str | Path) -> tuple[RequestCase, ...]:
    """Read the documented JSON request-corpus format from `path`."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = document.get("cases") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        raise ValueError("port verification corpus must be a JSON list or an object with 'cases'")
    cases: list[RequestCase] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"port verification case {index} must be a JSON object")
        record = cast(dict[str, Any], row)
        allowed = {"name", "method", "path", "headers", "body", "body_base64"}
        unknown = sorted(set(record) - allowed)
        if unknown:
            raise ValueError(
                f"port verification case {index} has unknown key(s): {', '.join(unknown)}"
            )
        if "body" in record and "body_base64" in record:
            raise ValueError(
                f"port verification case {index} must use body or body_base64, not both"
            )
        headers = record.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in headers.items()
        ):
            raise ValueError(
                f"port verification case {index} headers must map strings to strings"
            )
        body_text = record.get("body", "")
        encoded = record.get("body_base64")
        if encoded is not None:
            if not isinstance(encoded, str):
                raise ValueError(
                    f"port verification case {index} body_base64 must be a string"
                )
            try:
                body = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise ValueError(
                    f"port verification case {index} body_base64 is not valid base64"
                ) from error
        else:
            if not isinstance(body_text, str):
                raise ValueError(f"port verification case {index} body must be a string")
            body = body_text.encode("utf-8")
        try:
            name = record["name"]
            method = record["method"]
            request_path = record["path"]
        except KeyError as error:
            raise ValueError(
                f"port verification case {index} needs name, method, and path"
            ) from error
        if not all(isinstance(value, str) for value in (name, method, request_path)):
            raise ValueError(
                f"port verification case {index} name, method, and path must be strings"
            )
        cases.append(
            RequestCase(
                name=name,
                method=method.upper(),
                path=request_path,
                headers=tuple(cast(dict[str, str], headers).items()),
                body=body,
            )
        )
    return tuple(cases)


__all__ = [
    "Difference",
    "RequestCase",
    "ResponseSnapshot",
    "VerificationReport",
    "load_cases",
    "verify_apps",
]
