from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

from wreath._audit.rules.code import _BY_ID
from wreath._audit.scan import scan_paths

CORPUS = Path(__file__).parent / "hardening_corpus"
_MARKER = re.compile(r"#\s*hardening-expect:\s*(?P<rules>[a-z0-9, -]+)")
CORPUS_FILES = sorted(CORPUS.glob("*.py"))


def _expected(path: Path) -> dict[int, set[str]]:
    marked: dict[int, set[str]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _MARKER.search(line)
        if match is not None:
            marked[number] = {rule.strip() for rule in match.group("rules").split(",")}
    return marked


def _found(path: Path) -> dict[int, set[str]]:
    grouped: dict[int, set[str]] = defaultdict(set)
    report = scan_paths([path], include_tests=True)
    for finding in report.findings:
        grouped[int(finding.location.split(":")[0])].add(finding.rule_id)
    return dict(grouped)


def test_the_corpus_is_not_empty() -> None:
    # A glob that matches nothing makes every parameterised test below vacuous,
    # and a vacuous suite is indistinguishable from a passing one.
    assert len(CORPUS_FILES) >= 8


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda p: p.name)
def test_findings_match_the_markers_exactly(path: Path) -> None:
    assert _found(path) == _expected(path)


@pytest.mark.parametrize(
    "path",
    [p for p in CORPUS_FILES if p.name.startswith("secure_")],
    ids=lambda p: p.name,
)
def test_the_correct_spelling_produces_nothing(path: Path) -> None:
    # Deliberately redundant with the test above. This one names the property in
    # its own right, so a corpus file that loses its markers cannot quietly turn
    # into a file nobody is checking.
    assert scan_paths([path], include_tests=True).findings == []


def test_every_insecure_file_has_a_secure_twin() -> None:
    insecure = {p.name[len("insecure_") :] for p in CORPUS_FILES if p.name.startswith("insecure_")}
    secure = {p.name[len("secure_") :] for p in CORPUS_FILES if p.name.startswith("secure_")}
    assert insecure == secure


def test_the_ruleset_still_covers_every_planted_defect_class() -> None:
    fired = {finding.rule_id for finding in scan_paths([CORPUS], include_tests=True).findings}
    assert {
        "sql-interpolation",  # injection through unmodified SQL
        "hardcoded-secret",  # a development key that shipped
        "weak-randomness",  # a token from a seeded generator
        "timing-unsafe-compare",  # a timing oracle on a shared secret
        "path-from-request",  # a hand-rolled path join
        "unsafe-archive-extract",  # extraction that trusts its members
        "unsafe-xml-parser",  # the stdlib SAX reader
        "dynamic-import",  # a dotted path resolved from a request body
        "mass-assignment",  # a request body walked onto an ORM row
        "case-mapped-authz",  # Unicode case mapping in a staff allow-list
        "template-from-request",  # a template compiled from request data
    } <= fired


def test_every_finding_names_a_catalogued_rule() -> None:
    for finding in scan_paths([CORPUS], include_tests=True).findings:
        assert finding.rule_id in _BY_ID
        assert _BY_ID[finding.rule_id].suggestion
