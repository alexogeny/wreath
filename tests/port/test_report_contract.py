import pytest

port = pytest.importorskip("wreath.port")

VALID_TAGS = {"translated", "needs-review", "unsupported"}


def test_report_shape_and_every_query_is_still_reported(corpus_app_roots):
    doc = port.analyze_all(corpus_app_roots).as_dict()
    assert set(doc) >= {"counts", "findings"}
    assert set(doc["counts"]) >= {"translated", "needs_review", "unsupported"}
    for finding in doc["findings"]:
        assert set(finding) >= {"file", "line", "construct", "tag", "rule_id", "message"}
        assert finding["tag"] in VALID_TAGS
        assert isinstance(finding["line"], int)

    query_findings = [f for f in doc["findings"] if f["construct"] == "orm_query"]
    assert query_findings, "corpus deliberately contains .objects. query calls"
    assert all(f["tag"] in {"translated", "needs-review", "unsupported"} for f in query_findings)
    # And the split is real: a corpus this varied must land on both sides.
    tags = {f["tag"] for f in query_findings}
    assert "translated" in tags and "needs-review" in tags
