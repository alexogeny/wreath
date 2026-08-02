from __future__ import annotations

from wreath._devtools.bench_report import _migration_section, _scenario_table


def _row(
    framework: str,
    latency: float,
    *,
    errors: int = 0,
    protocol: str = "http/1.1",
    samples: list[float] | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "framework": framework,
        "errors": errors,
        "load_generator": "h2load",
        "protocol": protocol,
        "latency_ms_p99": latency,
    }
    if samples is not None:
        row["_samples"] = {"latency_ms_p99": samples}
    return row


def test_an_errored_row_tied_with_the_best_result_is_not_marked_as_a_win() -> None:
    html = _scenario_table(
        [
            _row("best", 1.0),
            _row("slower", 2.0),
            _row("errored", 1.0, errors=1),
        ]
    )

    assert html.count('class="win"') == 1


def test_a_scenario_table_does_not_rank_across_protocols() -> None:
    html = _scenario_table(
        [_row("http1", 1.0), _row("http2", 2.0, protocol="h2")]
    )

    assert 'class="win"' not in html


def test_a_scenario_table_does_not_rank_overlapping_run_ranges() -> None:
    html = _scenario_table(
        [
            _row("nominal-best", 1.0, samples=[0.5, 2.5]),
            _row("nominal-second", 2.0, samples=[1.5, 2.5]),
        ]
    )

    assert 'class="win"' not in html


def test_migration_results_ignore_entries_without_a_median() -> None:
    html = _migration_section(
        [
            {
                "results": {
                    "measured": {"median_ns": 10.0},
                    "metadata": {"operations": 3},
                    "foreign": "not a result",
                }
            }
        ]
    )

    assert "measured" in html
    assert "metadata" not in html
    assert "foreign" not in html
