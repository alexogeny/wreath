from __future__ import annotations

from typing import Any

from ._json import dumps as _json_dumps
from ._native import _core
from .linkset import LinkContext, Linkset, LinksetResponse, LinkTarget
from .response import Response

CATALOG_PATH = "/.well-known/api-catalog"
CATALOG_MEDIA_TYPE = b'application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"'
CATALOG_LINK = (
    b"link",
    b'</.well-known/api-catalog>; rel="api-catalog"; type="application/linkset+json"',
)


def catalog_path(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a path string or None")
    if not value.startswith("/"):
        raise ValueError(f"{name} must begin with '/'; pass '/{value}'")
    return value


def _absolute(request: Any, path: str) -> str:
    try:
        raw_host = request._single_header(b"host")
    except ValueError:
        return path
    if raw_host is None:
        return path
    host = raw_host.decode("latin-1")
    if _core.normalize_host(host, False) is None:
        return path
    return f"{request.scheme}://{host}{path}"


def catalog_response(
    request: Any,
    *,
    api_path: str,
    spec_path: str | None,
    docs_path: str | None,
) -> Response:
    links: dict[str, tuple[LinkTarget, ...]] = {}
    if spec_path is not None:
        links["service-desc"] = (
            LinkTarget(_absolute(request, spec_path), type="application/json"),
        )
    if docs_path is not None:
        links["service-doc"] = (LinkTarget(_absolute(request, docs_path), type="text/html"),)
    if not links:
        return Response(
            _json_dumps({"linkset": [{"anchor": _absolute(request, api_path)}]}),
            headers=[CATALOG_LINK],
            media_type=CATALOG_MEDIA_TYPE,
        )
    return LinksetResponse(
        Linkset(LinkContext(anchor=_absolute(request, api_path), links=links)),
        profile="https://www.rfc-editor.org/info/rfc9727",
        headers=[CATALOG_LINK],
    )
