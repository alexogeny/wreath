"""Port intermediate representation: findings, tags, and the coverage report.

Phase 0 is a pure-stdlib static analyzer, so ``_port`` defines its OWN lightweight
frozen records here rather than importing ``wreath.typegen.model`` at runtime — the
analyzer must be runnable without importing the ``wreath`` package (or its native
``_core``). # TODO: converge with the typegen IR (Diagnostic/TypeRef) once the tool
runs inside a built wreath, per design 07 §1.
"""
from __future__ import annotations

from dataclasses import dataclass

# Finding tags (the human-facing vocabulary; hyphenated per design 07 §3).
TRANSLATED = "translated"
NEEDS_REVIEW = "needs-review"
UNSUPPORTED = "unsupported"

VALID_TAGS = frozenset({TRANSLATED, NEEDS_REVIEW, UNSUPPORTED})

# JSON ``counts`` keys are underscored (a stable machine contract).
_COUNT_KEY = {TRANSLATED: "translated", NEEDS_REVIEW: "needs_review", UNSUPPORTED: "unsupported"}


@dataclass(frozen=True)
class Finding:
    """One recognized construct, its translation verdict, and where it lives."""

    file: str
    line: int
    construct: str
    tag: str
    rule_id: str
    message: str
    category: str

    def to_json(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "construct": self.construct,
            "tag": self.tag,
            "rule_id": self.rule_id,
            "message": self.message,
            "category": self.category,
        }


class Report:
    """Aggregate of findings with coverage math and JSON/markdown renderings."""

    __slots__ = ("findings", "roots")

    def __init__(self, findings: list[Finding], roots: list[str] | None = None) -> None:
        # Deterministic ordering: by file, then line, then rule — so re-runs and
        # merged reports are byte-stable (idempotency, design 07 §3).
        self.findings = sorted(findings, key=lambda f: (f.file, f.line, f.rule_id, f.construct))
        self.roots = list(roots or [])

    # -- counts ---------------------------------------------------------------
    @property
    def recognized_constructs(self) -> int:
        return len(self.findings)

    def _count(self, tag: str) -> int:
        return sum(1 for f in self.findings if f.tag == tag)

    def counts(self) -> dict:
        return {_COUNT_KEY[t]: self._count(t) for t in (TRANSLATED, NEEDS_REVIEW, UNSUPPORTED)}

    # -- coverage -------------------------------------------------------------
    def coverage(self, category: str) -> float:
        """translated / recognized within ``category`` (1.0 if none recognized)."""
        in_cat = [f for f in self.findings if f.category == category]
        if not in_cat:
            return 1.0
        translated = sum(1 for f in in_cat if f.tag == TRANSLATED)
        return translated / len(in_cat)

    def coverage_overall(self) -> float:
        if not self.findings:
            return 1.0
        return self._count(TRANSLATED) / len(self.findings)

    def categories(self) -> dict:
        cats: dict[str, dict] = {}
        for f in self.findings:
            slot = cats.setdefault(
                f.category, {"translated": 0, "needs_review": 0, "unsupported": 0}
            )
            slot[_COUNT_KEY[f.tag]] += 1
        return cats

    # -- renderings -----------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "roots": self.roots,
            "counts": self.counts(),
            "coverage_overall": round(self.coverage_overall(), 4),
            "categories": {
                cat: {**slot, "coverage": round(self.coverage(cat), 4)}
                for cat, slot in sorted(self.categories().items())
            },
            "findings": [f.to_json() for f in self.findings],
        }

    def to_markdown(self) -> str:
        c = self.counts()
        lines = [
            "# wreath port — analysis report",
            "",
            f"- recognized constructs: **{self.recognized_constructs}**",
            f"- translated: **{c['translated']}**  ·  needs-review: **{c['needs_review']}**  "
            f"·  unsupported: **{c['unsupported']}**",
            f"- overall auto-translatable: **{self.coverage_overall() * 100:.0f}%**",
            "",
            "## Coverage by category",
            "",
            "| category | translated | needs-review | unsupported | coverage |",
            "| --- | --- | --- | --- | --- |",
        ]
        for cat, slot in sorted(self.categories().items()):
            lines.append(
                f"| {cat} | {slot['translated']} | {slot['needs_review']} | "
                f"{slot['unsupported']} | {self.coverage(cat) * 100:.0f}% |"
            )
        lines += ["", "## Findings needing review or unsupported", ""]
        flagged = [f for f in self.findings if f.tag != TRANSLATED]
        if not flagged:
            lines.append("_none_")
        for f in flagged:
            lines.append(f"- `{f.tag}` **{f.construct}** — {f.file}:{f.line} — "
                         f"{f.message} _[{f.rule_id}]_")
        return "\n".join(lines) + "\n"

    @classmethod
    def merge(cls, reports: list[Report]) -> Report:
        findings: list[Finding] = []
        roots: list[str] = []
        for r in reports:
            findings.extend(r.findings)
            roots.extend(r.roots)
        return cls(findings, roots)
