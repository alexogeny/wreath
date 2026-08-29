"""Optional dev-only `AuditMiddleware`.

Off by default (never mounted unless the app opts in). When mounted, it runs the a11y
rules over outgoing `text/html` responses and logs findings — intended for local
development, not production. It never rewrites the response and swallows its own errors,
so it can never change behaviour or break a response; the cost is only paid when mounted.
"""

from __future__ import annotations

import logging

from .dom import parse_html
from .rules import A11Y_RULES

_LOG = logging.getLogger("wreath.audit")


class AuditMiddleware:
    """Mount with `app.add_middleware(AuditMiddleware())` in development only."""

    global_scope = True
    __slots__ = ("_logger",)

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or _LOG

    async def after(self, request, response):
        try:
            body = getattr(response, "body", None)
            if not isinstance(body, (bytes, bytearray)):
                return response
            headers = getattr(response, "headers", None) or []
            content_type = b""
            for key, value in headers:
                if key.lower() == b"content-type":
                    content_type = value
                    break
            if b"text/html" not in content_type.lower():
                return response
            root = parse_html(bytes(body).decode("utf-8", "replace"))
            findings = [f for rule in A11Y_RULES for f in rule(root, "response")]
            if findings:
                path = getattr(request, "path", "?")
                self._logger.warning("wreath audit: %d a11y finding(s) on %s", len(findings), path)
                for f in findings:
                    self._logger.warning(
                        "  %s %s %s: %s", f.severity.value, f.rule_id, f.location, f.message
                    )
        except Exception:  # noqa: BLE001 — a dev aid must never break the response
            pass
        return response
