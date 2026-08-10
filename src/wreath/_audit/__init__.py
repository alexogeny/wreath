"""`wreath audit` — accessibility (WCAG 2.1 A/AA) and performance auditing for the
HTML and responses Wreath generates.

Zero-dependency and offline (a dev/CI tool, never on a request path), so the
HTML is parsed with the standard-library `html.parser`. It covers a curated a11y
ruleset (incl. WCAG 1.4.3 contrast over the design tokens) plus middleware/size/render
performance checks over the API-docs surface and static HTML trees, `--fix` remediation
of the safe subset, a runtime HTTP mode (`run_runtime_audit`), and an opt-in dev
`AuditMiddleware`.
"""
from .fix import apply_fixes
from .middleware import AuditMiddleware
from .model import Finding, Report, Severity
from .runtime import audit_response, run_runtime_audit
from .sources import discover_static_dirs, run_audit

__all__ = [
    "Finding", "Report", "Severity", "run_audit", "discover_static_dirs",
    "apply_fixes", "AuditMiddleware", "run_runtime_audit", "audit_response",
]
