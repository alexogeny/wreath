"""Port intermediate representation: findings, tags, and the coverage report.

Phase 0 is a pure-stdlib static analyzer, so `_port` defines its OWN lightweight
frozen records here rather than importing `wreath.typegen.model` at runtime — the
analyzer must be runnable without importing the `wreath` package (or its native
`_core`). # TODO: converge with the typegen IR (Diagnostic/TypeRef) once the tool
runs inside a built wreath, per design 07 §1.
"""
from __future__ import annotations

from dataclasses import dataclass

# Finding tags (the human-facing vocabulary; hyphenated per design 07 §3).
TRANSLATED = "translated"
NEEDS_REVIEW = "needs-review"
UNSUPPORTED = "unsupported"

VALID_TAGS = frozenset({TRANSLATED, NEEDS_REVIEW, UNSUPPORTED})

# JSON `counts` keys are underscored (a stable machine contract).
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


@dataclass(frozen=True)
class SkippedFile:
    """A path the analyzer could not read, decode, or parse — and why.

    A silently skipped file is how a coverage number becomes a lie: the coverage
    denominator counts constructs found in files that *were* analyzed, so every
    file missing from that population has to be visible next to the number.
    `reason` is a stable machine code (see `analyzer._SKIP_REASONS`);
    `detail` is the exception's own message, for a human.
    """

    file: str
    reason: str
    detail: str

    def to_json(self) -> dict:
        return {"file": self.file, "reason": self.reason, "detail": self.detail}


class Report:
    """Aggregate of findings with coverage math and JSON/markdown renderings.

    **Coverage is undefined, not 1.0, when nothing was recognized.** `coverage`
    and `coverage_overall` return `None` for an empty denominator, and every
    rendering here prints `n/a` for it. A tree the analyzer understood nothing
    of is the case where the tool has failed hardest, and "100% auto-translatable"
    is the single most misleading thing it could say there.

    **Skipped files sit outside the coverage fraction entirely.** They contribute
    to neither numerator nor denominator — a file that could not be parsed has no
    constructs to classify, and inventing a verdict for it would be a guess. So
    coverage answers "of what I could read, how much carries across", and
    `files_analyzed`/`skipped` say how much of the tree that sentence covers.
    """

    __slots__ = ("findings", "roots", "skipped", "files_analyzed")

    def __init__(
        self,
        findings: list[Finding],
        roots: list[str] | None = None,
        skipped: list[SkippedFile] | None = None,
        files_analyzed: int = 0,
    ) -> None:
        # Deterministic ordering: by file, then line, then rule — so re-runs and
        # merged reports are byte-stable (idempotency, design 07 §3).
        self.findings = sorted(findings, key=lambda f: (f.file, f.line, f.rule_id, f.construct))
        self.roots = list(roots or [])
        self.skipped = sorted(skipped or [], key=lambda s: (s.file, s.reason))
        self.files_analyzed = files_analyzed

    # -- counts ---------------------------------------------------------------
    @property
    def recognized_constructs(self) -> int:
        return len(self.findings)

    def _count(self, tag: str) -> int:
        return sum(1 for f in self.findings if f.tag == tag)

    def counts(self) -> dict:
        return {_COUNT_KEY[t]: self._count(t) for t in (TRANSLATED, NEEDS_REVIEW, UNSUPPORTED)}

    # -- coverage -------------------------------------------------------------
    def coverage(self, category: str) -> float | None:
        """translated / recognized within `category`; `None` if none recognized.

        `None` rather than `1.0`: an empty denominator means the analyzer
        recognized nothing here, which is the absence of an answer and not a
        perfect score. Callers must render it as "n/a" (see `_percent`).
        """
        in_cat = [f for f in self.findings if f.category == category]
        if not in_cat:
            return None
        translated = sum(1 for f in in_cat if f.tag == TRANSLATED)
        return translated / len(in_cat)

    def coverage_overall(self) -> float | None:
        """translated / recognized across every category; `None` if none recognized."""
        if not self.findings:
            return None
        return self._count(TRANSLATED) / len(self.findings)

    def rule_counts(self) -> list[tuple[str, str, str, int]]:
        """`(rule_id, category, tag, count)` for the non-translated findings.

        The report lists findings one per line, in file order. That answers "what
        does this file need" and hides "what does this *codebase* need" -- a rule
        firing forty times across thirty files reads as forty unrelated problems.
        Clustering by rule is how the ported-app population gets prioritised, and
        it is the view that kept getting rewritten by hand against `--json`.

        Translated findings are excluded: they are the part that needs no
        decision, so ranking them ranks work nobody has to do. Heaviest first,
        then by rule id so equal counts are stable.
        """
        tally: dict[tuple[str, str, str], int] = {}
        for f in self.findings:
            if f.tag == TRANSLATED:
                continue
            tally[(f.rule_id, f.category, f.tag)] = tally.get((f.rule_id, f.category, f.tag), 0) + 1
        return [
            (rule, cat, tag, n)
            for (rule, cat, tag), n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

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
        overall = self.coverage_overall()
        return {
            "roots": self.roots,
            "files_analyzed": self.files_analyzed,
            "counts": self.counts(),
            # `null` when nothing was recognized. A consumer that formats this
            # blindly gets "None"/"null" in its output, which is loud; the old
            # 1.0 was silently wrong, which is worse.
            "coverage_overall": None if overall is None else round(overall, 4),
            "categories": {
                cat: {**slot, "coverage": _round(self.coverage(cat))}
                for cat, slot in sorted(self.categories().items())
            },
            "findings": [f.to_json() for f in self.findings],
            "skipped": [s.to_json() for s in self.skipped],
        }

    def to_markdown(self) -> str:
        c = self.counts()
        lines = [
            "# wreath port — analysis report",
            "",
            f"- files analyzed: **{self.files_analyzed}**  ·  "
            f"skipped: **{len(self.skipped)}**",
            f"- recognized constructs: **{self.recognized_constructs}**",
            f"- translated: **{c['translated']}**  ·  needs-review: **{c['needs_review']}**  "
            f"·  unsupported: **{c['unsupported']}**",
        ]
        if self.coverage_overall() is None:
            lines.append(
                "- overall auto-translatable: **n/a** — nothing was recognized, so there "
                "is no coverage to report (this is a failed analysis, not a perfect one)"
            )
        else:
            lines.append(f"- overall auto-translatable: **{_percent(self.coverage_overall())}**")
        lines += [
            "",
            "## Coverage by category",
            "",
            "| category | translated | needs-review | unsupported | coverage |",
            "| --- | --- | --- | --- | --- |",
        ]
        for cat, slot in sorted(self.categories().items()):
            lines.append(
                f"| {cat} | {slot['translated']} | {slot['needs_review']} | "
                f"{slot['unsupported']} | {_percent(self.coverage(cat))} |"
            )
        lines += ["", "## Files that could not be analyzed", ""]
        if not self.skipped:
            lines.append("_none_")
        else:
            noun = "path was" if len(self.skipped) == 1 else "paths were"
            lines.append(
                f"{len(self.skipped)} {noun} skipped, so nothing in them is counted "
                "above — coverage describes only the files that were read."
            )
            lines.append("")
            for s in self.skipped:
                lines.append(f"- `{s.reason}` {s.file} — {s.detail}")
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
        skipped: list[SkippedFile] = []
        analyzed = 0
        for r in reports:
            findings.extend(r.findings)
            roots.extend(r.roots)
            skipped.extend(r.skipped)
            analyzed += r.files_analyzed
        return cls(findings, roots, skipped, analyzed)


def _percent(value: float | None) -> str:
    """Render a coverage fraction, or `n/a` when its denominator was empty."""
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
