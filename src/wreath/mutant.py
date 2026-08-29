"""`wreath.mutant` -- does your test suite watch the controls you declared?

A passing test proves your code does what you wrote. It does not prove your
test would notice if it stopped. Those are different claims, and the gap between
them is where authorization bugs live: the rate limit still returns 200, the
policy still evaluates, the field is still absent from the response -- and the
test that "covers" it would say exactly the same thing if the control were
deleted.

`wreath mutant` removes one declared control at a time and re-runs the tests
that reach it. A control whose removal nothing notices is reported. So is a
control no test reaches at all, separately, because "the suite would not notice"
and "the suite never looks" are different problems.

What makes this different from a general mutation tester is that Wreath owns the
declarations. An `AuthRequirement`, a Cedar policy, an MCP tool's gate, a CRUD
router's withheld field set and a rate limit's key are *objects*, not lines, so
the mutations can be phrased the way an incident report is:

    src/wreath/_auth/requirements.py
      :122   clause `self.second_factor is not None` in a compound or condition
             predicate.drop-operand in AuthRequirement.access_level

Typical use, from a project root:

    wreath mutant                       # mutate this project, run its tests
    wreath mutant --operators cedar declaration
    wreath mutant --path src/myapp/policies.py --format json

**It is a report, not a gate.** A surviving mutant is a question -- *would you
want a test to catch this?* -- and sometimes the answer is no.
"""

from __future__ import annotations

from ._mutant.model import FINDINGS, Mutation, Outcome, Report, Site, Verdict
from ._mutant.operators import CONTROL_KEYWORDS, CONTROL_TOKENS, OPERATORS
from ._mutant.report import render, render_json
from ._mutant.runner import Plan, build_plan, execute

__all__ = [
    "CONTROL_KEYWORDS",
    "CONTROL_TOKENS",
    "FINDINGS",
    "Mutation",
    "OPERATORS",
    "Outcome",
    "Plan",
    "Report",
    "Site",
    "Verdict",
    "build_plan",
    "execute",
    "render",
    "render_json",
]
