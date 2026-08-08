"""Performance rules: HTML-surface budgets plus app-level checks derived by
introspecting the loaded application's middleware stack and OpenAPI document.

HTML rules are `(root, html, surface) -> Iterator[Finding]` in `HTML_PERF_RULES`;
app-level rules run once against the application in `app_perf`.
"""
from __future__ import annotations

from collections.abc import Iterator

from ..model import Finding, Severity

HTML_PERF_RULES: list = []

_HTML_BUDGET = 100 * 1024      # 100 KiB per document
_JSON_BUDGET = 512 * 1024      # 512 KiB OpenAPI doc
_INLINE_BUDGET = 16 * 1024     # per un-nonced inline <style>/<script>


def _rule(fn):
    HTML_PERF_RULES.append(fn)
    return fn


def _f(rule_id, severity, surface, message, reference, node=None, suggestion=""):
    return Finding(rule_id, severity, surface, message, reference,
                   node.loc if node else "", suggestion)


@_rule
def html_size(root, html, surface) -> Iterator[Finding]:
    size = len(html.encode("utf-8"))
    if size > _HTML_BUDGET:
        yield _f("html-size", Severity.WARN, surface,
                 f"document is {size // 1024} KiB (budget {_HTML_BUDGET // 1024} KiB)",
                 "perf:html-size", suggestion="trim or paginate the document")


@_rule
def img_dims(root, html, surface) -> Iterator[Finding]:
    for img in root.find_all("img"):
        if not (img.has_attr("width") and img.has_attr("height")):
            yield _f("img-dims", Severity.WARN, surface,
                     "<img> is missing width/height (causes layout shift)",
                     "perf:cls", img, suggestion="set explicit width and height")


@_rule
def render_blocking(root, html, surface) -> Iterator[Finding]:
    head = root.first("head")
    if head is None:
        return
    for node in head.walk():
        if node.tag == "link" and (node.attr("rel") or "").lower() == "stylesheet":
            yield _f("render-blocking", Severity.WARN, surface,
                     "render-blocking <link rel=stylesheet> in <head>",
                     "perf:render-blocking", node,
                     suggestion="inline critical CSS or load stylesheets non-blocking")
        elif (node.tag == "script" and node.has_attr("src")
                and not (node.has_attr("defer") or node.has_attr("async"))):
            yield _f("render-blocking", Severity.WARN, surface,
                     "render-blocking <script> in <head> (no defer/async)",
                     "perf:render-blocking", node,
                     suggestion="add defer/async, or move the script to the end of <body>")


@_rule
def inline_asset(root, html, surface) -> Iterator[Finding]:
    for node in root.find_all("style", "script"):
        if node.tag == "script" and node.has_attr("src"):
            continue  # external script — render-blocking covers it
        size = len(node.text.encode("utf-8"))
        if size <= _INLINE_BUDGET or node.has_attr("nonce"):
            # a CSP-nonced inline asset (e.g. the API-docs shell) is intentional.
            continue
        yield _f("inline-asset", Severity.WARN, surface,
                 f"large un-nonced inline <{node.tag}> ({size // 1024} KiB)",
                 "perf:inline-asset", node,
                 suggestion="externalise the asset, or add a CSP nonce if it is intentional")


def _mounted(app) -> set[str]:
    names: set[str] = set()
    policy = getattr(app, "_http_policy", None)
    if policy is not None:
        names.update(type(component).__name__ for component in policy.components)
    for attr in ("_middleware", "_global_middleware"):
        for item in getattr(app, attr, []) or []:
            mw = item[-1] if isinstance(item, tuple) else item
            names.add(type(mw).__name__)
    return names


def app_perf(app, openapi_json: str) -> Iterator[Finding]:
    """App-level performance findings (middleware presence + OpenAPI size)."""
    mounted = _mounted(app)

    if "CompressionMiddleware" not in mounted:
        yield Finding("compression-enabled", Severity.WARN, "app",
                      "no CompressionMiddleware mounted; text responses are uncompressed",
                      "perf:compression", "",
                      "mount wreath.middleware.CompressionMiddleware")

    if "CacheControlMiddleware" not in mounted:
        yield Finding("cache-control", Severity.WARN, "app",
                      "no CacheControlMiddleware mounted; responses lack cache directives",
                      "perf:cache", "",
                      "mount CacheControlMiddleware or set per-mount cache_control on static()")

    if "SecurityHeadersPolicy" not in mounted:
        yield Finding("security-headers", Severity.WARN, "app",
                      "no SecurityHeadersPolicy mounted (CSP / X-Content-Type-Options / "
                      "Referrer-Policy / HSTS)", "perf:security-headers", "",
                      "configure HttpPolicy(security_headers=SecurityHeadersPolicy(...))")

    size = len(openapi_json.encode("utf-8"))
    if size > _JSON_BUDGET:
        yield Finding("json-size", Severity.WARN, "app",
                      f"OpenAPI document is {size // 1024} KiB (budget {_JSON_BUDGET // 1024} KiB)",
                      "perf:json-size", "", "trim descriptions/examples or split the API")
