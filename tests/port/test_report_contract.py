"""The port report JSON contract.

Skipped today; auto-activates when the tool ships. Pins the report shape from
design 07 §3 and the invariant that ormar queries are flagged, never translated.
"""
import pytest

port = pytest.importorskip("wreath.port")

VALID_TAGS = {"translated", "needs-review", "unsupported"}


def test_report_shape(corpus_app_roots):
    doc = port.analyze_all(corpus_app_roots).to_json()
    assert set(doc) >= {"counts", "findings"}
    assert set(doc["counts"]) >= {"translated", "needs_review", "unsupported"}
    for finding in doc["findings"]:
        assert set(finding) >= {"file", "line", "construct", "tag", "rule_id", "message"}
        assert finding["tag"] in VALID_TAGS
        assert isinstance(finding["line"], int)


def test_unsupported_queries_are_flagged_not_translated(corpus_app_roots):
    doc = port.analyze_all(corpus_app_roots).to_json()
    query_findings = [f for f in doc["findings"] if f["construct"] == "orm_query"]
    assert query_findings, "corpus deliberately contains .objects. query calls"
    assert all(f["tag"] == "unsupported" for f in query_findings)
