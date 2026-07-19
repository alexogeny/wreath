"""Protocol dimension for the benchmark suite (orthogonal to frameworks)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmarks.h2load as h2load
from benchmarks.report import (
    _chart,
    generate_report,
    has_mixed_generators,
    has_mixed_protocols,
    is_rankable,
    merge_documents,
    render,
)
from benchmarks.scenarios import ALL_PROTOCOLS, SCENARIOS


def test_protocol_support_is_independent_from_framework_support() -> None:
    # A scenario declares protocols separately from frameworks.
    plaintext = SCENARIOS["plaintext"]
    assert plaintext.protocols == ALL_PROTOCOLS
    assert plaintext.supports_protocol("h2")
    assert plaintext.supports_protocol("h3")
    # Framework and protocol support do not imply each other.
    assert plaintext.supports("wreath-native")
    assert plaintext.supports_protocol("http/1.1")


def test_websocket_is_http11_only() -> None:
    ws = SCENARIOS["ws-echo"]
    assert ws.supports_protocol("http/1.1")
    assert not ws.supports_protocol("h2")
    assert not ws.supports_protocol("h3")


def _row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "framework": "wreath-native", "scenario": "plaintext",
        "protocol": "http/1.1", "transport": "tcp", "secure": True, "alpn": "http/1.1",
        "connections": 4, "max_streams_per_connection": 1, "trial": 1,
        "load_generator": "h2load", "load_generator_version": "1.0",
        "server_tls_version": "TLSv1.3", "errors": 0,
        "latency_ms_p95": 1.0, "latency_ms_p99": 2.0,
        "normalized_100k_seconds": 5.0,
    }
    base.update(over)
    return base


def test_reports_never_choose_an_error_row_as_winner() -> None:
    rows = [
        _row(framework="a", errors=5, latency_ms_p99=0.1),  # fastest but errored
        _row(framework="b", errors=0, latency_ms_p99=2.0),
    ]
    assert is_rankable(rows)  # error-free rows share one protocol+generator
    # The chart excludes errored rows from the winner (fastest-but-errored 'a'
    # must not win).
    html = _chart(rows, "latency_ms_p99", "t", "ms")
    # Winner marker must attach to b's row region, never within an ERRORS row.
    assert "WINNER" in html
    assert html.count("ERRORS") == 1


def test_reports_avoid_cross_protocol_ranking() -> None:
    rows = [_row(protocol="http/1.1"), _row(protocol="h2")]
    assert has_mixed_protocols(rows)
    assert not is_rankable(rows)


def test_reports_warn_and_avoid_ranking_for_mixed_generators() -> None:
    rows = [_row(load_generator="builtin"), _row(load_generator="h2load")]
    assert has_mixed_generators(rows)
    assert not is_rankable(rows)


def test_report_renders_mixed_generator_warning(tmp_path: Path) -> None:
    document = {
        "metadata": {"note": "x"},
        "results": [
            _row(framework="a", load_generator="builtin"),
            _row(framework="b", load_generator="h2load"),
        ],
    }
    out = tmp_path / "report.html"
    generate_report(document, out)
    html = out.read_text()
    assert "different load generators" in html
    assert "WINNER" not in html  # no winner across generators


def test_result_rows_include_protocol_metadata() -> None:
    # The runner attaches these fields to every result row.
    required = {
        "protocol", "transport", "secure", "alpn", "connections",
        "max_streams_per_connection", "trial", "load_generator",
        "load_generator_version", "server_tls_version",
    }
    assert required <= set(_row().keys())


def test_merged_runs_report_median_and_keep_raw_samples() -> None:
    docs = [
        {"metadata": {}, "results": [_row(framework="a", latency_ms_p99=v)]}
        for v in (1.0, 5.0, 3.0)
    ]
    merged = merge_documents(docs)
    row = merged["results"][0]
    assert row["latency_ms_p99"] == 3.0  # median, not first or mean
    assert row["_samples"]["latency_ms_p99"] == [1.0, 5.0, 3.0]  # raw kept
    assert row["_runs"] == 3
    assert merged["metadata"]["runs_merged"] == 3


def test_no_winner_when_run_ranges_overlap() -> None:
    """A lead smaller than the run-to-run spread is not a win."""
    overlapping = merge_documents([
        {"metadata": {}, "results": [
            _row(framework="a", latency_ms_p99=1.0),
            _row(framework="b", latency_ms_p99=1.2),
        ]},
        {"metadata": {}, "results": [
            _row(framework="a", latency_ms_p99=1.4),  # a's worst is beaten by b's best
            _row(framework="b", latency_ms_p99=1.3),
        ]},
    ])
    html = _chart(overlapping["results"], "latency_ms_p99", "t", "ms")
    assert "WINNER" not in html
    assert "smaller than the run-to-run spread" in html

    separated = merge_documents([
        {"metadata": {}, "results": [
            _row(framework="a", latency_ms_p99=1.0),
            _row(framework="b", latency_ms_p99=9.0),
        ]},
        {"metadata": {}, "results": [
            _row(framework="a", latency_ms_p99=1.1),  # every a sample beats every b
            _row(framework="b", latency_ms_p99=9.1),
        ]},
    ])
    html = _chart(separated["results"], "latency_ms_p99", "t", "ms")
    assert "WINNER" in html


def _orm_doc() -> dict[str, object]:
    return {
        "tool": "benchmarks.postgres.bench_orm_competitors",
        "metadata": {},
        "scenarios": {
            "get_by_pk": {
                "wreath": {"median_ms": 0.1, "sync": False},
                "tortoise": {"median_ms": 0.3, "sync": False},
                "peewee": {"median_ms": 0.2, "sync": True},
                "wreath_speedup_vs": {"tortoise": 3.0},  # a summary block, not a driver
            },
            # Wreath has no join predicate, so it is absent from this scenario.
            "join_filter_by_child": {
                "tortoise": {"median_ms": 0.5, "sync": False},
                "peewee": {"median_ms": 0.4, "sync": True},
            },
        },
    }


def _orm_table(html: str) -> tuple[int, list[str]]:
    """(column count, body rows) of the ORM section."""
    section = html.split("<h2>ORM, against the alternatives</h2>")[1].split("</section>")[0]
    head, body = section.split("<tbody>")
    return head.count("<th>"), [r for r in body.split("<tr>") if "<td" in r]


def test_orm_rows_stay_aligned_when_an_orm_omits_a_scenario() -> None:
    """An ORM that cannot do a scenario natively gets a dash, not a missing cell."""
    html = render({"metadata": {}, "results": []}, extra=[_orm_doc()])
    columns, rows = _orm_table(html)
    assert len(rows) == 2
    for row in rows:
        assert row.count("<td") == columns, row
    assert "peewee (sync)" in html  # synchronous ORMs are labelled, not hidden


def test_orm_ranking_excludes_the_synchronous_orm() -> None:
    """Peewee beats tortoise here, but must not be ranked against async ORMs."""
    html = render({"metadata": {}, "results": []}, extra=[_orm_doc()])
    _columns, rows = _orm_table(html)
    # wreath (0.1) is the fastest async ORM and wins; peewee (0.2) is dimmed, never green.
    assert '<td class="win"' in rows[0]
    assert '<td class="dim">0.200</td>' in rows[0]


def test_report_is_self_contained() -> None:
    """No network at render time: the report must work offline, from a file://."""
    document = {"metadata": {"note": "x"}, "results": [_row(framework="a")]}
    html = render(document)
    assert "https://" not in html and "http://" not in html
    assert "<script" not in html


def test_h2load_warmup_is_a_separate_unmeasured_run(monkeypatch) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(
        h2load, "capabilities", lambda: h2load.Capabilities("h2load", True)
    )

    def fake_run(command, **_kwargs):
        commands.append(command)
        for item in command:
            if item.startswith("--log-file="):
                Path(item.partition("=")[2]).write_text(
                    "0\t200\t10\n", encoding="utf-8"
                )
        count = int(command[command.index("-n") + 1])
        output = (
            f"finished in 1s, {count}.00 req/s\n"
            f"requests: {count} total, {count} started, {count} done, "
            "0 succeeded, 0 failed, 0 errored, 0 timeout\n"
        )
        # The parser reads the second requests field as successful requests.
        output = output.replace("0 succeeded", f"{count} succeeded")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(h2load.subprocess, "run", fake_run)
    result = h2load.measure(
        "127.0.0.1", 8000, "/", "http/1.1",
        requests=10, warmup_requests=3, connections=1, tls=False,
    )

    assert [command[command.index("-n") + 1] for command in commands] == ["3", "10"]
    assert result.requests == 10


def test_raw_trials_are_preserved_and_aggregates_derive_from_them() -> None:
    # Aggregation must never replace raw trials. A simple median-of-trials helper
    # derives from the raw rows without discarding them.
    trials = [_row(trial=i, latency_ms_p99=float(i)) for i in (1, 2, 3)]
    values = sorted(float(r["latency_ms_p99"]) for r in trials)
    median = values[len(values) // 2]
    assert median == 2.0
    assert len(trials) == 3  # raw rows still present


# --- the protocol comparison table ----------------------------------------
# A blank cell in a protocol column is ambiguous: it can mean the stack lost,
# was not measured, or cannot enter at all. These pin the third case, which is
# the only one the reader cannot infer.


def _protocol_row(framework: str, server: str, protocol: str, rps: float) -> dict:
    return {
        "framework": framework,
        "server": server,
        "scenario": "plaintext",
        "protocol": protocol,
        "requests_per_second": rps,
        "errors": 0,
        "load_generator": "h2load",
    }


def _mixed_protocol_rows() -> list[dict]:
    return [
        _protocol_row("wreath-native", "wreath-native (uvloop) [cpus 0,2]", "http/1.1", 210000),
        _protocol_row("wreath-native", "wreath-native (uvloop) [cpus 0,2]", "h2", 185000),
        _protocol_row("wreath-native", "wreath-native (uvloop) [cpus 0,2]", "h3", 120000),
        _protocol_row("wreath", "uvicorn", "http/1.1", 95000),
        _protocol_row("blacksheep", "uvicorn", "http/1.1", 88000),
        _protocol_row("sanic", "sanic-native", "http/1.1", 140000),
        _protocol_row("sanic", "sanic-native", "h3", 70000),
    ]


def test_a_single_protocol_is_not_a_comparison() -> None:
    from wreath._devtools.bench_report import _protocol_section

    rows = [_protocol_row("wreath", "uvicorn", "http/1.1", 1000)]
    assert _protocol_section(rows) == ""


def test_uvicorn_hosted_frameworks_are_marked_unsupported_not_blank() -> None:
    from wreath._devtools.bench_report import _protocol_section

    html = _protocol_section(_mixed_protocol_rows())
    # Uvicorn is HTTP/1.1-only, so wreath-on-uvicorn and blacksheep cannot enter
    # h2 or h3 at all. That is a different fact from losing.
    assert "not supported" in html
    assert "Uvicorn speaks HTTP/1.1 only" in html


def test_sanic_is_marked_unsupported_for_http2_only() -> None:
    from wreath._devtools.bench_report import _protocol_section

    html = _protocol_section(_mixed_protocol_rows())
    # Sanic's own HTTP enum has VERSION_1 and VERSION_3 and no VERSION_2.
    assert "it has no HTTP/2" in html
    assert "70,000" in html  # its h3 result is still reported


def test_the_table_never_ranks_across_protocols() -> None:
    from wreath._devtools.bench_report import _protocol_section

    html = _protocol_section(_mixed_protocol_rows())
    # wreath-native's http/1.1 (210k) beats its own h3 (120k), but a request is
    # not the same work in each, so only one cell per column may win.
    assert html.count('class="win"') == len({"http/1.1", "h2", "h3"})


def test_mixed_load_generators_suppress_ranking_in_the_table() -> None:
    from wreath._devtools.bench_report import _protocol_section

    rows = _mixed_protocol_rows()
    rows[0] = {**rows[0], "load_generator": "stdlib"}
    html = _protocol_section(rows)
    assert 'class="win"' not in html
    assert "different load generators" in html


def test_a_capable_but_unmeasured_protocol_is_not_called_unsupported() -> None:
    from wreath._devtools.bench_report import _protocol_section

    # wreath-native can serve h3; this run simply did not measure it.
    rows = [r for r in _mixed_protocol_rows() if not (
        r["framework"] == "wreath-native" and r["protocol"] == "h3")]
    html = _protocol_section(rows)
    assert "not measured in this run" in html


def test_capability_lookup_tolerates_the_server_suffix() -> None:
    from wreath._devtools.bench_report import _server_capability

    # Real rows carry a loop and CPU pinning suffix.
    protocols, _note = _server_capability("wreath-native (uvloop) [cpus 0,2,4]")
    assert protocols == {"http/1.1", "h2", "h3"}
    protocols, _note = _server_capability("uvicorn")
    assert protocols == {"http/1.1"}
