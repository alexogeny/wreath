from __future__ import annotations

from wreath._devtools.bench_report import (
    _bars,
    _cedar_section,
    _migration_artifact_block,
    _migration_generation_block,
    _migration_section,
    _orm_section,
    _overview,
    _postgres_section,
    _protocol_section,
    _routing_backends_section,
    _routing_memory_section,
    _scenario_table,
    render,
)


def _row(
    framework: str,
    rps: float | None = None,
    *,
    scenario: str = "plain",
    protocol: str = "http/1.1",
    generator: str = "oha",
    errors: int = 0,
    latency: float | str | None = None,
    samples: dict[str, list[float]] | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "scenario": scenario,
        "framework": framework,
        "protocol": protocol,
        "load_generator": generator,
        "errors": errors,
        "server": framework,
    }
    if rps is not None:
        row["requests_per_second"] = rps
    if latency is not None:
        row["latency_ms_p95"] = latency
    if samples is not None:
        row["_samples"] = samples
    return row


def test_bars_refuse_absent_metrics_and_rank_both_directions() -> None:
    assert _bars([_row("empty")], "requests_per_second", "Throughput", "req/s") == ""

    lower = _bars(
        [_row("slow", latency=20.0), _row("fast", latency=10.0)],
        "latency_ms_p95",
        "Latency",
        "ms",
    )
    assert lower.index("fast") < lower.index("slow")
    assert lower.count("bar-win") == 1
    assert '<div class="who">fast</div><div class="track"><div class="bar bar-win"' in lower
    assert 'style="width:50.00%"' in lower
    assert "10.000 ms" in lower
    assert "within-noise" not in lower

    higher = _bars(
        [_row("slow", 10.0), _row("fast", 20.0)],
        "requests_per_second",
        "Throughput",
        "req/s",
        lower_better=False,
    )
    assert higher.index("fast") < higher.index("slow")
    assert higher.count("bar-win") == 1
    assert '<div class="who">fast</div><div class="track"><div class="bar bar-win"' in higher
    assert "20.000 req/s" in higher
    assert "within-noise" not in higher


def test_bars_show_errors_ranges_and_unresolved_comparisons_without_winners() -> None:
    rows = [
        _row(
            "nominal",
            20.0,
            samples={"requests_per_second": [10.0, 30.0]},
        ),
        _row(
            "overlap",
            10.0,
            samples={"requests_per_second": [5.0, 25.0]},
        ),
        _row("broken", 20.0, errors=2),
    ]
    html = _bars(rows, "requests_per_second", "A & B", "req/s", lower_better=False)
    assert "A &amp; B" in html
    assert "10.000–30.000" in html
    assert "within-noise" in html
    assert "WINNER" not in html
    assert html.count("bar-err") == 1
    assert html.count("ERRORS") == 1

    mixed = _bars(
        [_row("a", 10.0), _row("b", 20.0, protocol="h2")],
        "requests_per_second",
        "mixed",
        "req/s",
        lower_better=False,
    )
    assert "WINNER" not in mixed
    assert "within-noise" not in mixed

    mixed_overlap = _bars(
        [
            _row("a", 10.0, samples={"requests_per_second": [5.0, 15.0]}),
            _row(
                "b",
                20.0,
                protocol="h2",
                samples={"requests_per_second": [10.0, 25.0]},
            ),
        ],
        "requests_per_second",
        "mixed overlap",
        "req/s",
        lower_better=False,
    )
    assert "within-noise" not in mixed_overlap


def test_bars_handle_zero_scale_and_do_not_crown_an_errored_tie() -> None:
    html = _bars(
        [_row("valid", 0.0), _row("broken", 0.0, errors=1)],
        "requests_per_second",
        "zero",
        "req/s",
        lower_better=False,
    )
    assert html.count('style="width:1.00%"') == 2
    assert html.count("WINNER") == 1
    assert '<div class="who">valid</div>' in html
    assert "bar-win bar-err" not in html


def test_scenario_table_covers_columns_missing_values_formats_and_errors() -> None:
    assert _scenario_table([_row("none")]) == ""
    rows = [
        _row(
            "best",
            2_000.0,
            latency=1.25,
            samples={"requests_per_second": [1_900.0, 2_100.0]},
        ),
        _row("second", 1_000.0, latency=2.5),
        _row("broken", 4_000.0, latency="invalid", errors=1),
    ]
    html = _scenario_table(rows)
    assert "<th>req/s</th>" in html
    assert "<th>p95 latency</th>" in html
    assert "2,000" in html
    assert "1.250" in html
    assert 'title="runs: 1,900.000–2,100.000"' in html
    assert html.count('class="win"') == 2
    assert '<tr><td>best </td><td class="win"' in html
    assert '>2,000</td><td class="win">1.250</td>' in html
    assert html.count('<td class="dim">—</td>') == 1
    assert html.count("ERRORS") == 1


def test_scenario_table_requires_two_valid_comparable_values_for_a_win() -> None:
    single = _scenario_table([_row("only", 10.0)])
    assert 'class="win"' not in single

    errored_best = _scenario_table([_row("valid", 10.0), _row("broken", 20.0, errors=1)])
    assert 'class="win"' not in errored_best

    only_errored = _scenario_table([_row("broken", 10.0, errors=1)])
    assert 'class="win"' not in only_errored

    invalid = _scenario_table([_row("valid", latency=1.0), _row("foreign", latency="bad")])
    assert '<td class="dim">—</td>' in invalid

    mixed = _scenario_table([_row("http1", 10.0), _row("http2", 20.0, protocol="h2")])
    assert 'class="win"' not in mixed


def test_overview_covers_missing_rows_ranges_winners_and_unresolved_rows() -> None:
    assert _overview([_row("none")], ["plain"]) == ""
    rows = [
        _row("a", 100.0, samples={"requests_per_second": [90.0, 110.0]}),
        _row("b", 50.0, samples={"requests_per_second": [40.0, 60.0]}),
        _row("a", 25.0, scenario="mixed"),
        _row("b", 30.0, scenario="mixed", protocol="h2"),
        _row("a", None, scenario="missing"),
    ]
    html = _overview(rows, ["plain", "mixed", "absent", "missing"])
    assert html.count('class="win"') == 1
    assert 'title="runs: 90–110"' in html
    assert html.count("·&nbsp;unresolved") == 2
    assert html.count('<td class="dim">—</td>') == 4
    assert "<td>100</td>" not in html


def test_protocol_section_handles_empty_filtered_ranked_and_unranked_inputs() -> None:
    assert _protocol_section([]) == ""
    assert _protocol_section([_row("wreath", 10.0)]) == ""
    ranked = _protocol_section(
        [
            _row("wreath", 10.0),
            _row("wreath", 30.0),
            _row("wreath", 20.0, protocol="h2"),
            _row("other", 5.0),
            _row("other", 15.0, protocol="h2"),
        ]
    )
    assert "20" in ranked
    assert ranked.count('class="win"') == 2
    assert "different load generators" not in ranked

    unranked = _protocol_section(
        [_row("a", 10.0), _row("b", 20.0, protocol="h2", generator="h2load")]
    )
    assert 'class="win"' not in unranked
    assert "different load generators" in unranked


def test_routing_sections_refuse_empty_inputs_and_render_grouped_results() -> None:
    assert _routing_memory_section([]) == ""
    memory = _routing_memory_section(
        [
            {
                "raw": {
                    "python": [
                        {
                            "shape": "static",
                            "routes": 2,
                            "total_bytes": 2_097_152,
                            "vmhwm_bytes": 4_194_304,
                            "compiled_bytes": 1_048_576,
                            "lazy_bytes": 1_048_576,
                            "compile_seconds": 0.002,
                        }
                    ]
                }
            }
        ]
    )
    assert "static — 2 routes" in memory
    assert "2.0 MiB" in memory
    assert "4.0 MiB" in memory
    assert "2.0 ms" in memory

    assert _routing_backends_section([]) == ""
    backends = _routing_backends_section(
        [
            {
                "caveat": "same queries &amp; shape",
                "tables": [
                    {
                        "name": "static",
                        "description": "fixed",
                        "queries": 2,
                        "backends": {
                            "c-fast": {"raw_seconds": [0.001]},
                            "python": {"raw_seconds": [0.002]},
                        },
                    }
                ],
            }
        ]
    )
    assert "static — fixed" in backends
    assert "1.00 ms" in backends
    assert "500000 ns" in backends
    assert "same queries &amp;amp; shape" in backends


def test_orm_section_filters_summaries_marks_sync_and_keeps_rows_aligned() -> None:
    assert _orm_section([]) == ""
    html = _orm_section(
        [
            {
                "scenarios": {
                    "read": {
                        "wreath": {"median_ms": 1.0},
                        "sqlalchemy": {"median_ms": 2.0},
                        "peewee": {"median_ms": 1.0, "sync": True},
                        "syncslow": {"median_ms": 5.0, "sync": True},
                        "wreath_speedup_vs": "median_ms",
                    },
                    "foreign": {"sqlalchemy": {"median_ms": 3.0}},
                }
            }
        ]
    )
    assert "peewee (sync)" in html
    assert "syncslow (sync)" in html
    assert "<th>wreath</th>" in html
    assert "wreath (sync)" not in html
    assert "wreath_speedup_vs" not in html
    assert html.count('class="win"') == 2
    assert '<td class="dim">1.000</td>' in html
    assert "2.0x" in html
    assert html.count('<td class="dim">—</td>') == 4


def test_orm_section_handles_only_sync_and_zero_wreath_results() -> None:
    html = _orm_section(
        [
            {
                "scenarios": {
                    "sync-only": {"peewee": {"median_ms": 1.0, "sync": True}},
                    "zero": {
                        "wreath": {"median_ms": 0.0},
                        "sqlalchemy": {"median_ms": 1.0},
                    },
                }
            }
        ]
    )
    assert html.count('<td class="dim">—</td>') == 5
    assert "x</td>" not in html


def test_migration_sections_filter_foreign_shapes_and_render_optional_blocks() -> None:
    assert _migration_section([]) == ""
    documents = [
        {
            "fairness": "same state",
            "results": {
                "wreath": {"median_ns": 100.0},
                "alembic": {"median_ns": 200.0},
                "metadata": "median_ns",
            },
            "fleet": {"tenants": 10, "median_ns_per_tenant": 50.0},
            "generation": {
                "fairness": "same drift",
                "results": {
                    "wreath": {"median_ns": 250.0, "operations": 3},
                    "foreign": "ignored",
                },
            },
            "artifact": {
                "median_ns": 500.0,
                "bytes": 1024,
                "fairness": "same bytes",
            },
        },
        {"generation": "foreign", "artifact": {"bytes": 3}, "fleet": {}},
    ]
    html = _migration_section(documents)
    assert "metadata" not in html
    assert "same state" in html
    assert "10 already-current tenants" in html
    assert "20,000,000 tenants/s" in html
    assert "Plan generation" in html
    assert "3</td><td>250</td><td>4,000,000" in html
    assert "WMA1 artifact verification" in html
    assert "1,024-byte" in html
    assert "2,000,000 verifications/s" in html


def test_migration_optional_blocks_refuse_documents_without_measurements() -> None:
    assert _migration_generation_block([{"generation": []}]) == ""
    assert _migration_generation_block([{"generation": {"results": {}}}]) == ""
    assert _migration_artifact_block([{"artifact": []}, {"artifact": {"bytes": 2}}]) == ""


def test_cedar_section_filters_foreign_values_and_ranks_only_stateless_work() -> None:
    assert _cedar_section([]) == ""
    html = _cedar_section(
        [
            {
                "fairness": "same policies",
                "skipped": {"cedarpy": "not installed"},
                "evaluate": {
                    "wreath": {"median_ns": 100.0},
                    "cedarpy": {"median_ns": 200.0},
                    "foreign": "median_ns",
                    "missing": {},
                },
                "parse_and_evaluate": {
                    "wreath": {"median_ns": 100.0},
                    "cedarpy": {"median_ns": 200.0},
                },
            }
        ]
    )
    assert "foreign" not in html
    assert "same policies" in html
    assert "skipped (not installed)" in html
    assert html.count('class="win"') == 2
    assert html.count('class="lose"') == 2
    evaluate = html.split("Parse and evaluate", 1)[0]
    assert 'class="win"' not in evaluate
    assert 'class="lose"' not in evaluate


def test_postgres_section_filters_foreign_values_and_handles_optional_ratios() -> None:
    assert _postgres_section([]) == ""
    html = _postgres_section(
        [
            {
                "scenarios": {
                    "read": {
                        "wreath": {"median_ms": 1.0},
                        "asyncpg": {"median_ms": 2.0},
                        "foreign": "ignored",
                    },
                    "other": {"asyncpg": {"median_ms": 3.0}},
                    "zero": {
                        "wreath": {"median_ms": 0.0},
                        "asyncpg": {"median_ms": 1.0},
                    },
                }
            }
        ]
    )
    assert "foreign" not in html
    assert "2.00x" in html
    assert html.count('<td class="dim">—</td>') == 1
    assert html.count("wreath vs asyncpg") == 1


def test_render_covers_empty_single_merged_mixed_and_noted_documents() -> None:
    empty = render({"metadata": {}, "results": []})
    assert "Hover any value for its range across runs." in empty

    rows = [
        _row("a", 20.0, scenario="template"),
        _row("b", 10.0, scenario="template", protocol="h2"),
    ]
    single = render({"metadata": {}, "results": rows})
    assert "A single run." in single
    assert '<section class="chart">' in single
    assert "Not an engine race." in single
    assert "Rows span multiple protocols" in single
    assert "Protocols, and who can speak them" in single

    mixed = render(
        {
            "metadata": {"runs_merged": 2},
            "results": [rows[0], _row("b", 10.0, scenario="template", generator="wrk")],
        }
    )
    assert "Medians across 2 merged runs." in mixed
    assert "Rows use different load generators" in mixed

    comparable = render({"metadata": {}, "results": [_row("a", 20.0), _row("b", 10.0)]})
    assert "Rows span multiple protocols" not in comparable


def test_render_omits_row_sections_and_uses_bespoke_documents_when_needed() -> None:
    html = render(
        {"metadata": {}, "results": []},
        [
            {
                "scenarios": {
                    "read": {
                        "wreath": {"median_ms": 1.0},
                        "asyncpg": {"median_ms": 2.0},
                    }
                }
            }
        ],
    )
    assert "PostgreSQL driver" in html
    assert '<section class="scenario">' not in html
