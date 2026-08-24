"""Stage 6 of first-class logging: OTLP logs, the stdlib bridge, the doctor check.

`_otlp.py` already builds trace and metric requests; logs is the third signal on
the same transport, and the bounded queue, exporter thread and failure isolation
around it already exist. This stage is the mapping and the interop, not new
plumbing.

The bridge is opt-in on purpose. A framework that grabs the root logger fights
`dictConfig`, surprises anyone with existing handlers, and either double-emits or
silently discards their configuration. Instead `wreath.doctor` grows a check that
notices the split-stream situation and says so.
"""

from __future__ import annotations

import logging as stdlib_logging

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logsink import ProjectedLog
from wreath._otlp import build_logs_request


@pytest.fixture
def runtime() -> log.LogRuntime:
    with log.testing_runtime(level=log.TRACE):
        yield log.installed()


def _site() -> log.LogEvent:
    return log.event(
        "export.denied",
        "user {user} denied {resource}",
        level=log.WARN,
        fields=(log.field("user", int), log.field("resource", str, log.RAW)),
    )


def _record(site: log.LogEvent, **kw: object) -> ProjectedLog:
    cell = fs.LogCell(
        request_id=1,
        site_id=site.site_id,
        severity=fs.Severity.WARN,
        args=(fs.LogArg.integer(17), fs.LogArg.text("orders")),
    )
    return ProjectedLog(cell=cell, observed_unix_nano=1_700_000_000_000_000_000, **kw)  # type: ignore[arg-type]


# --- OTLP logs mapping ------------------------------------------------------


def test_an_empty_batch_builds_an_empty_request(runtime: log.LogRuntime) -> None:
    assert build_logs_request([], registry=runtime.registry) == {"resourceLogs": []}


def test_a_record_maps_onto_the_otel_log_fields(runtime: log.LogRuntime) -> None:
    site = _site()
    request = build_logs_request(
        [_record(site, trace_id=(1 << 64) | 2, span_id=3)], registry=runtime.registry
    )
    (resource_logs,) = request["resourceLogs"]
    (scope_logs,) = resource_logs["scopeLogs"]
    (record,) = scope_logs["logRecords"]

    assert record["severityNumber"] == int(fs.Severity.WARN)
    assert record["severityText"] == "WARN"
    assert record["body"]["stringValue"] == "user 17 denied orders"
    assert record["eventName"] == "export.denied"
    assert record["traceId"] == f"{(1 << 64) | 2:032x}"
    assert record["spanId"] == f"{3:016x}"
    assert record["observedTimeUnixNano"] == "1700000000000000000"
    assert record["timeUnixNano"] == "1700000000000000000"


def test_declared_arguments_become_attributes(runtime: log.LogRuntime) -> None:
    site = _site()
    request = build_logs_request([_record(site)], registry=runtime.registry)
    record = request["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attrs = {a["key"]: a["value"] for a in record["attributes"]}
    assert attrs["user"] == {"intValue": "17"}
    assert attrs["resource"] == {"stringValue": "orders"}


def test_a_record_without_correlation_omits_the_ids(runtime: log.LogRuntime) -> None:
    """OTLP forbids an all-zero trace id; absent is the correct encoding."""
    site = _site()
    request = build_logs_request([_record(site)], registry=runtime.registry)
    record = request["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert "traceId" not in record
    assert "spanId" not in record


def test_the_resource_carries_the_service_name(runtime: log.LogRuntime) -> None:
    site = _site()
    request = build_logs_request(
        [_record(site)],
        registry=runtime.registry,
        resource_attributes={"service.name": "orders-api"},
    )
    attrs = {
        a["key"]: a["value"]["stringValue"]
        for a in request["resourceLogs"][0]["resource"]["attributes"]
    }
    assert attrs["service.name"] == "orders-api"


def test_records_share_one_scope_entry(runtime: log.LogRuntime) -> None:
    """OTLP groups records under a shared scope rather than repeating it."""
    site = _site()
    request = build_logs_request(
        [_record(site), _record(site)], registry=runtime.registry
    )
    (scope_logs,) = request["resourceLogs"][0]["scopeLogs"]
    assert len(scope_logs["logRecords"]) == 2
    assert scope_logs["scope"]["name"]


def test_a_redacted_argument_never_reaches_otlp(runtime: log.LogRuntime) -> None:
    site = log.event("export.token", "token {token}", fields=(log.field("token", str),))
    site("hunter2")
    cell = fs.LogCell(
        request_id=0,
        site_id=site.site_id,
        severity=fs.Severity.INFO,
        args=(fs.LogArg.hashed(0xABCD),),
    )
    request = build_logs_request([ProjectedLog(cell=cell)], registry=runtime.registry)
    assert "hunter2" not in repr(request)


def test_dropped_siblings_are_exported_as_an_attribute(
    runtime: log.LogRuntime,
) -> None:
    """What the limiter suppressed must be visible to the collector too."""
    site = _site()
    cell = fs.LogCell(
        request_id=0,
        site_id=site.site_id,
        severity=fs.Severity.INFO,
        dropped_siblings=12,
    )
    request = build_logs_request([ProjectedLog(cell=cell)], registry=runtime.registry)
    record = request["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attrs = {a["key"]: a["value"] for a in record["attributes"]}
    assert attrs["wreath.dropped_siblings"] == {"intValue": "12"}


# --- the stdlib bridge ------------------------------------------------------


def test_the_bridge_forwards_a_stdlib_record(runtime: log.LogRuntime) -> None:
    logger = stdlib_logging.getLogger("test.bridge.forward")
    logger.propagate = False
    logger.setLevel(stdlib_logging.DEBUG)
    with log.stdlib_bridge(logger) as records:
        logger.warning("something broke")
    assert len(records) == 1
    assert records[0].severity == fs.Severity.WARN


def test_the_bridge_maps_stdlib_levels_onto_severity_bands(
    runtime: log.LogRuntime,
) -> None:
    logger = stdlib_logging.getLogger("test.bridge.levels")
    logger.propagate = False
    logger.setLevel(stdlib_logging.DEBUG)
    with log.stdlib_bridge(logger) as records:
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        logger.critical("c")
    assert [c.severity for c in records] == [
        fs.Severity.DEBUG,
        fs.Severity.INFO,
        fs.Severity.WARN,
        fs.Severity.ERROR,
        fs.Severity.FATAL,
    ]


def test_the_bridge_carries_the_logger_name_as_the_event(
    runtime: log.LogRuntime,
) -> None:
    logger = stdlib_logging.getLogger("asyncpg.pool")
    logger.propagate = False
    logger.setLevel(stdlib_logging.DEBUG)
    with log.stdlib_bridge(logger) as records:
        logger.info("connection established")
    assert log.render(records[0]).endswith("connection established")


def test_the_bridge_hashes_the_formatted_message_by_default(
    runtime: log.LogRuntime,
) -> None:
    """A foreign library's message is an undeclared string, so it follows the
    same deny-by-default rule as everything else -- unless asked otherwise."""
    logger = stdlib_logging.getLogger("test.bridge.redact")
    logger.propagate = False
    logger.setLevel(stdlib_logging.DEBUG)
    with log.stdlib_bridge(logger, raw_messages=False) as records:
        logger.info("token=hunter2")
    assert b"hunter2" not in records[0].encode()


def test_the_bridge_detaches_on_exit(runtime: log.LogRuntime) -> None:
    logger = stdlib_logging.getLogger("test.bridge.detach")
    logger.propagate = False
    logger.setLevel(stdlib_logging.DEBUG)
    with log.stdlib_bridge(logger) as records:
        logger.info("inside")
    logger.info("outside")
    assert len(records) == 1


def test_the_bridge_records_join_the_current_request(runtime: log.LogRuntime) -> None:
    """The reason to bridge at all: a third-party library's records land in the
    same correlated stream as everything else."""
    logger = stdlib_logging.getLogger("test.bridge.request")
    logger.propagate = False
    logger.setLevel(stdlib_logging.DEBUG)
    with log.stdlib_bridge(logger) as records:
        with log.request_scope(request_id=77) as scope:
            logger.warning("during a request")
            scope.finish(promoted=False)
    assert records[0].request_id == 77


# --- the doctor check -------------------------------------------------------


def test_doctor_reports_a_foreign_logger_holding_its_own_handlers() -> None:
    from wreath.doctor import check_logging_streams

    logger = stdlib_logging.getLogger("test.doctor.foreign")
    logger.addHandler(stdlib_logging.NullHandler())
    try:
        with log.testing_runtime():
            findings = check_logging_streams()
    finally:
        logger.handlers.clear()
    assert any("test.doctor.foreign" in f for f in findings)


def test_doctor_is_quiet_when_the_bridge_is_installed() -> None:
    from wreath.doctor import check_logging_streams

    logger = stdlib_logging.getLogger("test.doctor.bridged")
    logger.addHandler(stdlib_logging.NullHandler())
    try:
        with log.testing_runtime(), log.stdlib_bridge(logger):
            findings = check_logging_streams()
    finally:
        logger.handlers.clear()
    assert not any("test.doctor.bridged" in f for f in findings)


def test_doctor_says_nothing_when_wreath_logging_is_inactive() -> None:
    from wreath.doctor import check_logging_streams

    logger = stdlib_logging.getLogger("test.doctor.inactive")
    logger.addHandler(stdlib_logging.NullHandler())
    try:
        assert check_logging_streams(active=False) == []
    finally:
        logger.handlers.clear()


# --- the export tick --------------------------------------------------------


def test_the_pipeline_exports_logs_on_its_tick(runtime: log.LogRuntime) -> None:
    from wreath._export import ExportPipeline

    site = _site()
    sent: list[dict] = []

    class Transport:
        def export_traces(self, request: dict) -> None: ...
        def export_metrics(self, request: dict) -> None: ...
        def export_logs(self, request: dict) -> None:
            sent.append(request)

    pipeline = ExportPipeline(Transport(), log_registry=runtime.registry)
    pipeline.on_log(_record(site))
    pipeline._tick()
    assert len(sent) == 1
    assert sent[0]["resourceLogs"]
    assert pipeline.stats["exported_logs"] == 1


def test_an_empty_log_tick_does_not_probe_the_transport(
    runtime: log.LogRuntime,
) -> None:
    from wreath._export import ExportPipeline

    class Transport:
        def export_traces(self, request: dict) -> None: ...
        def export_metrics(self, request: dict) -> None: ...

        @property
        def export_logs(self):
            raise AssertionError("an empty queue reached the exporter")

    pipeline = ExportPipeline(Transport(), log_registry=runtime.registry)

    pipeline._tick()

    assert pipeline.stats["exported_logs"] == 0
    assert pipeline.stats["log_errors"] == 0


def test_a_transport_without_export_logs_is_counted_not_crashed(
    runtime: log.LogRuntime,
) -> None:
    """A transport predating the logs signal must degrade to a rising number."""
    from wreath._export import ExportPipeline

    class OldTransport:
        def export_traces(self, request: dict) -> None: ...
        def export_metrics(self, request: dict) -> None: ...

    pipeline = ExportPipeline(OldTransport(), log_registry=runtime.registry)
    pipeline.on_log(_record(_site()))
    pipeline._tick()
    assert pipeline.stats["log_errors"] == 1


def test_a_raising_log_exporter_is_isolated(runtime: log.LogRuntime) -> None:
    from wreath._export import ExportPipeline

    class Flaky:
        def export_traces(self, request: dict) -> None: ...
        def export_metrics(self, request: dict) -> None: ...
        def export_logs(self, request: dict) -> None:
            raise OSError("collector down")

    pipeline = ExportPipeline(Flaky(), log_registry=runtime.registry)
    pipeline.on_log(_record(_site()))
    pipeline._tick()
    assert pipeline.stats["log_errors"] == 1
    assert pipeline.stats["exported_logs"] == 0


def test_logs_and_traces_do_not_share_a_queue(runtime: log.LogRuntime) -> None:
    """A log burst must not evict the traces an operator came for."""
    from wreath._export import ExportPipeline

    class Transport:
        def export_traces(self, request: dict) -> None: ...
        def export_metrics(self, request: dict) -> None: ...
        def export_logs(self, request: dict) -> None: ...

    pipeline = ExportPipeline(Transport(), log_registry=runtime.registry, queue_capacity=2)
    site = _site()
    for _ in range(10):
        pipeline.on_log(_record(site))
    assert pipeline.queue.dropped == 0  # the trace queue is untouched
    assert pipeline.stats["log_dropped"] == 8


def test_dropped_siblings_is_exported_only_when_something_was_dropped(
    runtime: log.LogRuntime,
) -> None:
    """`wreath.dropped_siblings` says how many events the limiter ate.

    Every record carries the counter, and it is zero on the overwhelming
    majority -- so exporting it unconditionally puts a `0` attribute on every
    log line in the system, which costs a field per record on the wire and
    tells a reader nothing. The guard that keeps it off was never exercised
    from the other side: no test built a record with a non-zero count, so the
    attribute could have been absent *always* and the suite stayed green.
    """
    site = _site()

    def attributes(cell_kw: dict[str, object]) -> dict[str, object]:
        cell = fs.LogCell(
            request_id=1,
            site_id=site.site_id,
            severity=fs.Severity.WARN,
            args=(fs.LogArg.integer(17), fs.LogArg.text("orders")),
            **cell_kw,  # type: ignore[arg-type]
        )
        record = ProjectedLog(cell=cell, observed_unix_nano=1_700_000_000_000_000_000)
        built = build_logs_request([record], registry=runtime.registry)
        entry = built["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        return {a["key"]: a["value"] for a in entry["attributes"]}

    quiet = attributes({})
    assert "wreath.dropped_siblings" not in quiet

    throttled = attributes({"dropped_siblings": 12})
    assert throttled["wreath.dropped_siblings"] == {"intValue": "12"}
