from __future__ import annotations

from typing import Any

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logsite import SiteRegistry
from wreath.recording import CaptureDisposition


@pytest.fixture
def sink() -> list[fs.LogCell]:
    """Install a capturing sink and a fresh site registry for one test."""
    records: list[fs.LogCell] = []
    with log.testing_runtime(records.append):
        yield records


@pytest.mark.parametrize("capacity", [True, 1.5, float("nan"), float("inf")])
def test_site_registry_capacity_requires_a_positive_integer(capacity: Any) -> None:
    with pytest.raises(log.LogSiteError, match="site capacity must be a positive integer"):
        SiteRegistry(capacity)


@pytest.mark.parametrize("capacity", [0, True, 1.5, float("nan"), float("inf")])
def test_site_registry_replacement_capacity_requires_a_positive_integer(capacity: Any) -> None:
    registry = SiteRegistry(1)
    with pytest.raises(log.LogSiteError, match="site capacity must be a positive integer"):
        registry.set_capacity(capacity)


def test_site_registry_accepts_a_positive_replacement_capacity() -> None:
    registry = SiteRegistry(1)
    registry.set_capacity(2)
    assert registry.capacity == 2


def test_event_registration_returns_a_callable_site(sink: list[fs.LogCell]) -> None:
    denied = log.event(
        "auth.denied",
        "user {user} denied {resource}",
        level=log.WARN,
        fields=(log.field("user", int), log.field("resource", str, log.RAW)),
    )
    denied(17, "orders")
    assert len(sink) == 1
    assert sink[0].site_id == denied.site_id
    assert sink[0].severity == fs.Severity.WARN
    assert sink[0].args == (fs.LogArg.integer(17), fs.LogArg.text("orders"))


def test_site_ids_are_dense_and_distinct(sink: list[fs.LogCell]) -> None:
    first = log.event("a.one", "one", level=log.INFO)
    second = log.event("a.two", "two", level=log.INFO)
    assert first.site_id != second.site_id
    assert {first.site_id, second.site_id} == {1, 2}  # 0 means uninterned


def test_registration_rejects_an_unsupported_field_type() -> None:
    with pytest.raises(log.LogSiteError, match="cannot be packed"):
        log.event("bad.type", "x {v}", fields=(log.field("v", dict),))


def test_registration_rejects_a_template_that_names_an_undeclared_field() -> None:
    with pytest.raises(log.LogSiteError, match="resource"):
        log.event(
            "bad.template",
            "user {user} denied {resource}",
            fields=(log.field("user", int),),
        )


def test_registration_rejects_a_declared_field_the_template_never_uses() -> None:
    with pytest.raises(log.LogSiteError, match="unused"):
        log.event("bad.unused", "user {user}", fields=(log.field("user", int), log.field("x", int)))


def test_registration_rejects_more_fields_than_a_cell_holds() -> None:
    fields = tuple(log.field(f"f{i}", int) for i in range(fs.LOG_MAX_ARGS + 1))
    template = " ".join(f"{{f{i}}}" for i in range(fs.LOG_MAX_ARGS + 1))
    with pytest.raises(log.LogSiteError, match="at most"):
        log.event("bad.width", template, fields=fields)


def test_registering_the_same_event_name_twice_is_refused() -> None:
    with log.testing_runtime(lambda _cell: None):
        log.event("dup.name", "a {v}", fields=(log.field("v", int),))
        with pytest.raises(log.LogSiteError, match="already registered"):
            log.event("dup.name", "b {v}", fields=(log.field("v", int),))


def test_an_undeclared_string_field_is_hashed(sink: list[fs.LogCell]) -> None:
    site = log.event("redact.default", "token {token}", fields=(log.field("token", str),))
    site("hunter2")
    (cell,) = sink
    assert cell.args[0].type is fs.LogArgType.HASH
    assert cell.flags & fs.LOG_FLAG_REDACTED
    assert b"hunter2" not in cell.encode()


def test_a_declared_raw_string_field_is_verbatim(sink: list[fs.LogCell]) -> None:
    site = log.event("redact.raw", "route {route}", fields=(log.field("route", str, log.RAW),))
    site("/orders")
    assert sink[0].args == (fs.LogArg.text("/orders"),)
    assert not sink[0].flags & fs.LOG_FLAG_REDACTED


def test_scalars_are_raw_without_declaring_a_disposition(sink: list[fs.LogCell]) -> None:
    site = log.event(
        "redact.scalar",
        "n {n} f {f} b {b}",
        fields=(log.field("n", int), log.field("f", float), log.field("b", bool)),
    )
    site(5, 1.5, True)
    assert sink[0].args == (
        fs.LogArg.integer(5),
        fs.LogArg.real(1.5),
        fs.LogArg.boolean(True),
    )


def test_hashing_is_stable_within_a_process(sink: list[fs.LogCell]) -> None:
    site = log.event("redact.stable", "t {t}", fields=(log.field("t", str),))
    site("same")
    site("same")
    site("other")
    assert sink[0].args[0] == sink[1].args[0]
    assert sink[0].args[0] != sink[2].args[0]


def test_a_length_disposition_keeps_only_the_length(sink: list[fs.LogCell]) -> None:
    site = log.event(
        "redact.length", "body {body}", fields=(log.field("body", str, CaptureDisposition.LENGTH),)
    )
    site("abcdefgh")
    assert sink[0].args == (fs.LogArg.length(8),)


def test_a_disabled_site_is_falsey_and_emits_nothing() -> None:
    with log.testing_runtime(lambda _c: None, level=log.INFO) as records:
        chatty = log.event("gate.debug", "x {v}", level=log.DEBUG, fields=(log.field("v", int),))
        loud = log.event("gate.warn", "y {v}", level=log.WARN, fields=(log.field("v", int),))
        assert not chatty
        assert loud
        chatty(1)
        loud(2)
    assert [c.severity for c in records] == [fs.Severity.WARN]


class _Explosive:
    """A value that raises if anything tries to pack it."""

    def __str__(self) -> str:
        raise AssertionError("a disabled log call must not touch its arguments")


def test_a_disabled_call_does_not_pack_its_arguments() -> None:
    with log.testing_runtime(lambda _c: None, level=log.WARN):
        chatty = log.event("gate.nopack", "v {v}", level=log.DEBUG, fields=(log.field("v", str),))
        chatty(_Explosive())


def test_a_disabled_kwargs_call_does_not_intern_a_site() -> None:
    with log.testing_runtime(lambda _c: None, level=log.WARN) as records:
        log.debug("never seen {v}", v=_Explosive())
        log.warn("seen {v}", v=1)
    assert len(records) == 1
    assert records[0].site_id == 1  # the disabled call minted no site


def test_the_guard_and_the_call_agree(sink: list[fs.LogCell]) -> None:
    site = log.event("gate.agree", "v {v}", level=log.INFO, fields=(log.field("v", int),))
    if site:
        site(1)
    assert len(sink) == 1


def test_kwargs_tier_emits_and_interns(sink: list[fs.LogCell]) -> None:
    log.info("cache miss for {key}", key=7)
    log.info("cache miss for {key}", key=8)
    assert len(sink) == 2
    assert sink[0].site_id == sink[1].site_id != 0
    assert sink[0].args == (fs.LogArg.integer(7),)


def test_kwargs_tier_hashes_strings_and_keeps_scalars(sink: list[fs.LogCell]) -> None:
    log.warn("charge {attempt} via {gateway}", attempt=2, gateway="stripe")
    (cell,) = sink
    assert cell.severity == fs.Severity.WARN
    assert cell.args[0] == fs.LogArg.integer(2)
    assert cell.args[1].type is fs.LogArgType.HASH
    assert b"stripe" not in cell.encode()


def test_kwargs_tier_severities(sink: list[fs.LogCell]) -> None:
    with log.testing_runtime(sink.append, level=log.TRACE):
        log.trace("a")
        log.debug("b")
        log.info("c")
        log.warn("d")
        log.error("e")
        log.fatal("f")
    assert [c.severity for c in sink] == [
        fs.Severity.TRACE,
        fs.Severity.DEBUG,
        fs.Severity.INFO,
        fs.Severity.WARN,
        fs.Severity.ERROR,
        fs.Severity.FATAL,
    ]


def test_kwargs_tier_respects_the_level(sink: list[fs.LogCell]) -> None:
    with log.testing_runtime(sink.append, level=log.WARN):
        log.info("quiet")
        log.error("loud")
    assert len(sink) == 1


def test_kwargs_tier_keys_on_template_text_not_identity(sink: list[fs.LogCell]) -> None:
    for _ in range(3):
        template = "".join(["dynamic ", "{v}"])
        log.info(template, v=1)
    assert len({c.site_id for c in sink}) == 1


def test_site_table_overflow_is_counted_and_records_stay_uninterned() -> None:
    with log.testing_runtime(lambda _c: None, site_capacity=2) as records:
        log.info("one {v}", v=1)
        log.info("two {v}", v=1)
        log.info("three {v}", v=1)
        assert log.site_overflow_count() == 1
    assert [c.site_id for c in records] == [1, 2, 0]


def test_emitting_without_a_runtime_does_not_raise() -> None:
    site = log.event("noruntime.site", "v {v}", fields=(log.field("v", int),))
    site(1)
    log.info("no runtime yet {v}", v=1)


def test_a_type_mismatch_is_counted_not_raised(sink: list[fs.LogCell]) -> None:
    site = log.event("mismatch.site", "n {n}", fields=(log.field("n", int),))
    site("not an int")
    assert len(sink) == 1
    assert sink[0].args == (fs.LogArg.none(),)
    assert log.type_mismatch_count() == 1


def test_too_many_positional_arguments_is_counted_not_raised(sink: list[fs.LogCell]) -> None:
    site = log.event("arity.site", "n {n}", fields=(log.field("n", int),))
    site(1, 2, 3)
    assert len(sink) == 1
    assert sink[0].args == (fs.LogArg.integer(1),)
    assert log.arity_mismatch_count() == 1


def test_too_few_positional_arguments_pads_rather_than_raising(sink: list[fs.LogCell]) -> None:
    site = log.event(
        "arity.short", "a {a} b {b}", fields=(log.field("a", int), log.field("b", int))
    )
    site(1)
    assert sink[0].args == (fs.LogArg.integer(1), fs.LogArg.none())
    assert log.arity_mismatch_count() == 1


def test_a_site_renders_its_record_from_the_template(sink: list[fs.LogCell]) -> None:
    site = log.event(
        "render.site",
        "user {user} denied {resource}",
        level=log.WARN,
        fields=(log.field("user", int), log.field("resource", str, log.RAW)),
    )
    site(17, "orders")
    assert log.render(sink[0]) == "user 17 denied orders"


def test_rendering_a_redacted_argument_shows_a_fingerprint(sink: list[fs.LogCell]) -> None:
    site = log.event("render.redacted", "token {token}", fields=(log.field("token", str),))
    site("hunter2")
    rendered = log.render(sink[0])
    assert "hunter2" not in rendered
    assert rendered.startswith("token ")


def test_rendering_an_unknown_site_says_so(sink: list[fs.LogCell]) -> None:
    orphan = fs.LogCell(request_id=0, site_id=9999, severity=fs.Severity.INFO)
    assert "9999" in log.render(orphan)


def test_a_site_exposes_its_declared_names_for_structured_output(
    sink: list[fs.LogCell],
) -> None:
    site = log.event(
        "attrs.site",
        "user {user} on {route}",
        fields=(log.field("user", int), log.field("route", str, log.RAW)),
    )
    site(3, "/orders")
    assert log.attributes(sink[0]) == {"user": 3, "route": "/orders"}
