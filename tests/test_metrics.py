"""Every subsystem's counters, collected once and exported.

Two dozen objects in this tree keep counters, each added with a written reason
an operator would want it, and none of them reached a dashboard: the shipped
Prometheus/StatsD/CloudWatch bridges read the *flight projector's* snapshot --
route aggregates -- and nothing read a subsystem.

These tests pin the seam. The properties that matter are the ones that make a
scrape trustworthy rather than merely present:

* collection is by *asking*, so a new subsystem is not a new place to forget;
* two instances of one subsystem stay two series, or a deployment running four
  queues gets one number that means nothing;
* one subsystem raising must not blank every other subsystem's numbers;
* the exposition stays parseable -- a scraper rejects a family whose samples
  are not contiguous, which is the one way this can be silently broken.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath, metrics
from wreath._prometheus import PrometheusBridge, render_exposition
from wreath._statsd import StatsDBridge
from wreath.metrics import Counters


class FakeProjector:
    """The route-aggregate half, which these tests are not about."""

    def snapshot(self) -> Any:
        return type(
            "S", (), {"assembled": 0, "pending": 0, "loss": None, "routes": ()}
        )()


def _app() -> Wreath:
    app = Wreath()
    app.postgres("main", dsn="postgresql://u:p@localhost/db")
    app.messaging("events", database="main")
    app.jobs("work", database="main")
    app.jobs("mail", database="main")
    app.entities(database="main", bus="events")
    return app


# --- collection -----------------------------------------------------------------------


def test_every_registered_subsystem_reports() -> None:
    found = {reading.subsystem for reading in metrics.collect(_app())}
    assert {"jobs", "messaging", "entity", "pool"} <= found


def test_two_instances_of_one_subsystem_stay_apart() -> None:
    # A deployment runs several queues. One number covering all of them is a
    # number nobody can act on.
    queues = {r.instance for r in metrics.collect(_app()) if r.subsystem == "jobs"}
    assert queues == {"work", "mail"}


def test_an_app_with_nothing_registered_reports_nothing() -> None:
    assert metrics.collect(Wreath()) == ()


def test_collection_is_by_asking_not_by_a_list() -> None:
    # The property that keeps this true as subsystems are added: anything the
    # application holds that offers `counters()` contributes, with no registry
    # to update. Proved by attaching one to a registry the walk already reaches.
    class Counting:
        def counters(self) -> Counters:
            return Counters(subsystem="invented", instance="x", values={"n": 3})

    app = _app()
    app._job_runners["extra"] = Counting()  # type: ignore[assignment]
    readings = {(r.subsystem, r.instance): r for r in metrics.collect(app)}
    assert readings[("invented", "x")].values == {"n": 3}


def test_a_subsystem_that_raises_does_not_blank_the_others() -> None:
    # A metrics read must not be able to take down the thing it measures, and
    # one bug must not cost every other subsystem its numbers.
    class Exploding:
        def counters(self) -> Counters:
            raise RuntimeError("counter overflowed into the sea")

    app = _app()
    app._job_runners["broken"] = Exploding()  # type: ignore[assignment]
    found = {reading.subsystem for reading in metrics.collect(app)}
    assert {"jobs", "messaging", "entity", "pool"} <= found


def test_something_returning_the_wrong_shape_is_skipped() -> None:
    class Wrong:
        def counters(self) -> Any:
            return {"not": "a Counters"}

    app = Wreath()
    app._job_runners["odd"] = Wrong()  # type: ignore[assignment]
    assert metrics.collect(app) == ()


def test_a_holder_with_no_counters_is_simply_absent() -> None:
    app = Wreath()
    app._job_runners["plain"] = object()  # type: ignore[assignment]
    assert metrics.collect(app) == ()


# --- the flat form --------------------------------------------------------------------


def test_prefixed_names_carry_the_namespace_and_subsystem() -> None:
    reading = Counters(subsystem="jobs", instance="work", values={"run_errors": 2})
    assert reading.prefixed() == {"wreath_jobs_run_errors": 2}
    assert reading.prefixed("acme") == {"acme_jobs_run_errors": 2}


def test_flatten_sums_instances_because_a_flat_sink_cannot_hold_them_apart() -> None:
    # Stated rather than silent: `prefixed` deliberately leaves `instance` out,
    # so the caller choosing the flat form is the one choosing the sum.
    readings = (
        Counters(subsystem="jobs", instance="work", values={"run_errors": 2}),
        Counters(subsystem="jobs", instance="mail", values={"run_errors": 5}),
    )
    assert metrics.flatten(readings) == {"wreath_jobs_run_errors": 7}


# --- the Prometheus exposition --------------------------------------------------------


def test_counters_reach_the_exposition() -> None:
    text = PrometheusBridge(FakeProjector(), app=_app()).render()
    assert "wreath_jobs_run_errors{instance=\"work\"} 0" in text
    assert "wreath_entity_lost{instance=\"wreath_entity\"} 0" in text


def test_a_bridge_without_an_app_renders_the_projector_half_alone() -> None:
    # The projector half has to keep working for a caller that never wired an
    # app -- a test double, or a process exporting only route aggregates.
    text = PrometheusBridge(FakeProjector()).render()
    assert "wreath_jobs" not in text


def test_each_family_is_contiguous() -> None:
    """A scraper rejects an exposition whose samples are not grouped.

    Two queues interleaving would produce two `# HELP` blocks for one name,
    which is the one way this can break while still looking fine by eye.
    """
    text = PrometheusBridge(FakeProjector(), app=_app()).render()
    seen: list[str] = []
    for line in text.splitlines():
        if line.startswith("# HELP "):
            family = line.split()[2]
            assert family not in seen, f"{family} declared twice"
            seen.append(family)


def test_every_counter_sample_is_labelled_by_instance() -> None:
    text = PrometheusBridge(FakeProjector(), app=_app()).render()
    rows = [
        line for line in text.splitlines()
        if line.startswith("wreath_jobs_") and not line.startswith("#")
    ]
    assert rows and all('instance="' in row for row in rows)


def test_counters_render_as_gauges() -> None:
    """Not counters, and the difference is not cosmetic.

    A reading may be monotonic (`jobs.run_errors`) or may move both ways
    (`pool.borrowed`), and this layer cannot tell them apart. Declaring a
    falling series a counter makes a scraper read every decrease as a process
    restart and invent a rate spike out of it.
    """
    text = PrometheusBridge(FakeProjector(), app=_app()).render()
    types = [line for line in text.splitlines() if line.startswith("# TYPE wreath_pool_")]
    assert types and all(line.endswith(" gauge") for line in types)


def test_a_name_that_is_not_a_valid_metric_name_is_sanitised() -> None:
    readings = (Counters(subsystem="odd-one", instance="x", values={"a.b": 1}),)
    text = render_exposition(FakeProjector().snapshot(), counters=readings)
    assert "wreath_odd_one_a_b{instance=\"x\"} 1" in text


def test_rendering_no_counters_changes_nothing() -> None:
    plain = render_exposition(FakeProjector().snapshot())
    assert render_exposition(FakeProjector().snapshot(), counters=()) == plain


# --- the StatsD bridge ----------------------------------------------------------------


def test_statsd_emits_a_line_per_counter() -> None:
    bridge = StatsDBridge(FakeProjector(), app=_app(), dogstatsd=True)
    lines: list[str] = []
    bridge._counter_lines(lines)
    assert any(line.startswith("wreath.jobs.run_errors:") for line in lines)
    assert all(line.endswith("|g") or "|g|#" in line for line in lines)


def test_statsd_without_an_app_emits_nothing_extra() -> None:
    bridge = StatsDBridge(FakeProjector())
    lines: list[str] = []
    bridge._counter_lines(lines)
    assert lines == []


@pytest.mark.parametrize("dogstatsd", [True, False])
def test_statsd_carries_the_instance_either_way(dogstatsd: bool) -> None:
    # Dogstatsd carries it as a tag; plain StatsD has no tag dimension, so it
    # goes into the metric path instead. Losing it would merge two queues.
    bridge = StatsDBridge(FakeProjector(), app=_app(), dogstatsd=dogstatsd)
    lines: list[str] = []
    bridge._counter_lines(lines)
    assert any("work" in line for line in lines)


# --- exposition correctness `wreath mutant` found nothing watching --------------------
#
# These cover the renderer's own sanitisation and format rules rather than the
# counter seam above. Each was reported as a surviving control: the behaviour
# was reachable, correct, and unwatched.


@pytest.mark.parametrize(
    ("given", "expected"),
    [("9lives", "_9lives"), ("0", "_0"), ("ok", "ok"), ("_x", "_x")],
)
def test_a_metric_name_may_not_begin_with_a_digit(given: str, expected: str) -> None:
    # Prometheus rejects the exposition outright, so a subsystem named from
    # data -- a tenant id, a queue named "2024" -- would break the whole scrape
    # rather than its own line.
    readings = (Counters(subsystem=given, instance="x", values={"n": 1}),)
    text = render_exposition(FakeProjector().snapshot(), counters=readings)
    assert f"wreath_{expected}_n{{" in text


def test_a_label_name_that_is_empty_or_leads_with_a_digit_is_prefixed() -> None:
    from wreath._prometheus import _sanitize_label_name

    assert _sanitize_label_name("") == "_"
    assert _sanitize_label_name("9") == "_9"
    assert _sanitize_label_name("route_id") == "route_id"


def test_openmetrics_drops_the_total_suffix_from_the_family_only() -> None:
    # The samples keep `_total`; the family declaration does not. Getting this
    # backwards makes an OpenMetrics parser reject the document.
    snapshot = FakeProjector().snapshot()
    text = render_exposition(snapshot, openmetrics=True)
    assert "# TYPE wreath_http_requests counter" in text
    assert "# TYPE wreath_http_requests_total counter" not in text


def test_only_openmetrics_drops_it() -> None:
    text = render_exposition(FakeProjector().snapshot(), openmetrics=False)
    assert "# TYPE wreath_http_requests_total counter" in text


def test_openmetrics_terminates_the_document_and_prometheus_does_not() -> None:
    snapshot = FakeProjector().snapshot()
    assert render_exposition(snapshot, openmetrics=True).endswith("# EOF\n")
    assert not render_exposition(snapshot, openmetrics=False).endswith("# EOF\n")


def test_a_snapshot_with_no_routes_attribute_renders() -> None:
    # `getattr(snapshot, "routes", ()) or ()` -- a source that reports no routes
    # at all, which is every source before the first request.
    class Bare:
        assembled = 0
        pending = 0
        loss = None

    text = render_exposition(Bare())
    assert "wreath_http_requests_total" in text


def test_an_empty_histogram_bucket_emits_no_sample() -> None:
    """Only non-empty buckets are written, and the cumulative still carries them.

    A histogram with 64 buckets and two populated ones would otherwise put 64
    rows per route on the wire, which is the difference between a scrape that
    is free and one that is not.
    """
    buckets = [0] * 64
    buckets[3] = 5

    class Route:
        route_id = 1
        count = 5
        errors = 0
        duration_us_sum = 100
        duration_us_max = 50

    Route.buckets = buckets  # type: ignore[attr-defined]

    class WithRoute:
        assembled = 1
        pending = 0
        loss = None
        routes = (Route(),)

    text = render_exposition(WithRoute())
    rows = [line for line in text.splitlines() if "_bucket{" in line]
    # One populated bucket plus the mandatory +Inf, not sixty-five.
    assert len(rows) == 2
    assert any('le="+Inf"' in row for row in rows)


def test_a_snapshot_reporting_routes_as_none_renders() -> None:
    """`getattr(..., "routes", ()) or ()` — the `or` arm, not the default arm.

    A source that *has* the attribute and reports `None` is a different case
    from one that lacks it, and only the second was covered. Both bridges take
    the same shape, so both are checked here.
    """
    class NullRoutes:
        assembled = 0
        pending = 0
        loss = None
        routes = None

    text = render_exposition(NullRoutes())
    assert "wreath_http_requests_total" in text

    bridge = StatsDBridge(FakeProjector())
    assert bridge._lines(NullRoutes(), None) is not None
