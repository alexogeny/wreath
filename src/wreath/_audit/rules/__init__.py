"""Rule registries. Import the submodules for their registration side effects."""
from .a11y import A11Y_RULES
from .perf import HTML_PERF_RULES, app_perf

__all__ = ["A11Y_RULES", "HTML_PERF_RULES", "app_perf"]
