"""The port report JSON contract.

Skipped today; auto-activates when the tool ships. Pins the report shape from
design 07 §3 and the invariant that ormar queries are flagged, never translated.
"""
import pytest

port = pytest.importorskip("wreath.port")

VALID_TAGS = {"translated", "needs-review", "unsupported"}


def test_report_shape(corpus_app_roots):
    doc = port.analyze_all(corpus_app_roots).as_dict()
    assert set(doc) >= {"counts", "findings"}
    assert set(doc["counts"]) >= {"translated", "needs_review", "unsupported"}
    for finding in doc["findings"]:
        assert set(finding) >= {"file", "line", "construct", "tag", "rule_id", "message"}
        assert finding["tag"] in VALID_TAGS
        assert isinstance(finding["line"], int)


def test_every_query_is_still_reported(corpus_app_roots):
    """Design 07 §6: no ``.objects.`` chain goes unmentioned, whatever its verdict.

    This used to assert that no query is ever tagged ``translated``. That was a
    proxy for "the emitter does not rewrite queries", and it stopped being one
    once the analyzer began reading arguments: ``filter(id=x)`` has a fully
    determined target and says so, while the emitter still copies the body
    byte-for-byte. The rewrite contract is pinned directly in
    ``test_query_classification.test_the_emitter_never_rewrites_a_query``; what
    matters here is that every chain still produces a finding, because silence
    is the only genuinely useless verdict.
    """
    doc = port.analyze_all(corpus_app_roots).as_dict()
    query_findings = [f for f in doc["findings"] if f["construct"] == "orm_query"]
    assert query_findings, "corpus deliberately contains .objects. query calls"
    assert all(f["tag"] in {"translated", "needs-review", "unsupported"}
               for f in query_findings)
    # And the split is real: a corpus this varied must land on both sides.
    tags = {f["tag"] for f in query_findings}
    assert "translated" in tags and "needs-review" in tags
