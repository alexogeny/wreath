"""Finding / Report value types shared by the rules, runner, and CLI."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


# Sort key so reports read worst-first.
_ORDER = {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class Finding:
    """One audit result. `reference` is a WCAG success criterion (a11y) or a perf
    budget id; `location` is `line:col` within a surface, or empty for app-level
    findings; `surface` is `api-docs` / `static:<path>` / `app`."""

    rule_id: str
    severity: Severity
    surface: str
    message: str
    reference: str = ""
    location: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule_id,
            "severity": self.severity.value,
            "surface": self.surface,
            "reference": self.reference,
            "location": self.location,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings) -> None:
        self.findings.extend(findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    def sorted(self) -> list[Finding]:
        return sorted(
            self.findings, key=lambda f: (f.surface, _ORDER[f.severity], f.rule_id, f.location)
        )

    def to_json(self) -> dict:
        return {
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "total": len(self.findings),
            },
            "findings": [f.to_dict() for f in self.sorted()],
        }
