"""Port findings, tags, and coverage reports."""

from __future__ import annotations

from dataclasses import dataclass

TRANSLATED = "translated"
NEEDS_REVIEW = "needs-review"
UNSUPPORTED = "unsupported"

VALID_TAGS = frozenset({TRANSLATED, NEEDS_REVIEW, UNSUPPORTED})

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

    def as_dict(self) -> dict:
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

    def as_dict(self) -> dict:
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

    __slots__ = ("findings", "roots", "skipped", "files_analyzed", "detection")

    def __init__(
        self,
        findings: list[Finding],
        roots: list[str] | None = None,
        skipped: list[SkippedFile] | None = None,
        files_analyzed: int = 0,
        detection=None,
    ) -> None:
        # What framework the tree turned out to be. Optional so a Report built by
        # hand (tests, merges of older reports) still constructs; absent means
        # "not asked", which the renderings say rather than guess at.
        self.detection = detection
        # Deterministic ordering: by file, then line, then rule — so re-runs and
        # merged reports are byte-stable (idempotency, design 07 §3).
        self.findings = sorted(
            findings,
            key=lambda finding: (
                finding.file,
                finding.line,
                finding.rule_id,
                finding.construct,
            ),
        )
        self.roots = list(roots or [])
        self.skipped = sorted(
            skipped or [], key=lambda skipped_file: (skipped_file.file, skipped_file.reason)
        )
        self.files_analyzed = files_analyzed

    @property
    def recognized_constructs(self) -> int:
        return len(self.findings)

    def _count(self, tag: str) -> int:
        return sum(1 for finding in self.findings if finding.tag == tag)

    def counts(self) -> dict:
        return {
            _COUNT_KEY[tag]: self._count(tag) for tag in (TRANSLATED, NEEDS_REVIEW, UNSUPPORTED)
        }

    def coverage(self, category: str) -> float | None:
        """translated / recognized within `category`; `None` if none recognized.

        `None` rather than `1.0`: an empty denominator means the analyzer
        recognized nothing here, which is the absence of an answer and not a
        perfect score. Callers must render it as "n/a" (see `_percent`).
        """
        category_findings = [finding for finding in self.findings if finding.category == category]
        if not category_findings:
            return None
        translated = sum(1 for finding in category_findings if finding.tag == TRANSLATED)
        return translated / len(category_findings)

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
        for finding in self.findings:
            if finding.tag == TRANSLATED:
                continue
            key = (finding.rule_id, finding.category, finding.tag)
            tally[key] = tally.get(key, 0) + 1
        return [
            (rule, category, tag, count)
            for (rule, category, tag), count in sorted(
                tally.items(), key=lambda item: (-item[1], item[0])
            )
        ]

    def categories(self) -> dict:
        categories: dict[str, dict] = {}
        for finding in self.findings:
            slot = categories.setdefault(
                finding.category,
                {"translated": 0, "needs_review": 0, "unsupported": 0},
            )
            slot[_COUNT_KEY[finding.tag]] += 1
        return categories

    def as_dict(self) -> dict:
        overall = self.coverage_overall()
        return {
            "roots": self.roots,
            "files_analyzed": self.files_analyzed,
            # First key a reader should see after the roots: a coverage number
            # means nothing until you know whether the tree is even the kind of
            # application this tool ports.
            "detection": None if self.detection is None else self.detection.as_dict(),
            "counts": self.counts(),
            # `null` when nothing was recognized. A consumer that formats this
            # blindly gets "None"/"null" in its output, which is loud; the old
            # 1.0 was silently wrong, which is worse.
            "coverage_overall": None if overall is None else round(overall, 4),
            "categories": {
                cat: {**slot, "coverage": _round(self.coverage(cat))}
                for cat, slot in sorted(self.categories().items())
            },
            "findings": [finding.as_dict() for finding in self.findings],
            "skipped": [skipped_file.as_dict() for skipped_file in self.skipped],
        }

    def to_markdown(self) -> str:
        counts = self.counts()
        lines = [
            "# wreath port — analysis report",
            "",
        ]
        if self.detection is not None:
            lines.append(f"- stack detected: **{self.detection.headline()}**")
            for warning in self.detection.warnings():
                lines += ["", f"> **{warning}**", ""]
        lines += [
            f"- files analyzed: **{self.files_analyzed}**  ·  skipped: **{len(self.skipped)}**",
            f"- recognized constructs: **{self.recognized_constructs}**",
            f"- translated: **{counts['translated']}**  ·  "
            f"needs-review: **{counts['needs_review']}**  ·  "
            f"unsupported: **{counts['unsupported']}**",
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
            for skipped_file in self.skipped:
                lines.append(
                    f"- `{skipped_file.reason}` {skipped_file.file} — {skipped_file.detail}"
                )
        lines += ["", "## Findings needing review or unsupported", ""]
        flagged = [finding for finding in self.findings if finding.tag != TRANSLATED]
        if not flagged:
            lines.append("_none_")
        for finding in flagged:
            lines.append(
                f"- `{finding.tag}` **{finding.construct}** — "
                f"{finding.file}:{finding.line} — {finding.message} "
                f"_[{finding.rule_id}]_"
            )
        return "\n".join(lines) + "\n"

    @classmethod
    def merge(cls, reports: list[Report]) -> Report:
        findings: list[Finding] = []
        roots: list[str] = []
        skipped: list[SkippedFile] = []
        analyzed = 0
        for report in reports:
            findings.extend(report.findings)
            roots.extend(report.roots)
            skipped.extend(report.skipped)
            analyzed += report.files_analyzed
        from .detect import Detection

        return cls(
            findings,
            roots,
            skipped,
            analyzed,
            detection=Detection.merge([report.detection for report in reports]),
        )


def _percent(value: float | None) -> str:
    """Render a coverage fraction, or `n/a` when its denominator was empty."""
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
