from __future__ import annotations

from wreath._devtools.bench_report import classify, render


def document() -> dict[str, object]:
    return {
        "tool": "benchmarks.bench_migration_resolution",
        "results": {
            "wreath-metal": {"median_ns": 10.0},
            "alembic": {"median_ns": 100.0},
            "django": {"median_ns": 200.0},
        },
        "fleet": {
            "tool": "wreath-metal",
            "tenants": 10_000,
            "median_ns_per_tenant": 3.0,
        },
        "fairness": "Equivalent no-op plans; no DDL.",
    }


def test_migration_document_has_a_dedicated_report_kind() -> None:
    assert classify(document()) == "migration-resolution"


def test_migration_section_is_in_latest_html() -> None:
    html = render({"metadata": {}, "results": []}, [document()])

    assert "Migration resolution" in html
    assert "wreath-metal" in html
    assert "alembic" in html
    assert "django" in html
    assert "10,000 already-current tenants" in html
    assert "not catalog I/O or DDL" in html
