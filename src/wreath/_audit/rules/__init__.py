"""Rule registries. Import the submodules for their registration side effects."""
from .a11y import A11Y_RULES
from .code import CODE_RULES, scan_source
from .perf import HTML_PERF_RULES, app_perf
from .security import RESPONSE_SECURITY_RULES, ResponseView

__all__ = [
    "A11Y_RULES",
    "CODE_RULES",
    "HTML_PERF_RULES",
    "RESPONSE_SECURITY_RULES",
    "ResponseView",
    "app_perf",
    "scan_source",
]
