"""Implementation of `wreath.mutant`. Use the facade; this package is private.

Layout, in the order a run touches them:

* `operators` -- what a mutation *is*: the control vocabulary and the rewrites.
* `patch` -- how one is installed in a live interpreter without re-importing.
* `trace` -- which tests reach which line, via PEP 669.
* `runner` -- plan, baseline, one fork per mutant.
* `report` -- findings first, score last.
* `cli` -- `wreath mutant`.
"""

from .model import FINDINGS, Mutation, Outcome, Report, Site, Verdict

__all__ = ["FINDINGS", "Mutation", "Outcome", "Report", "Site", "Verdict"]
