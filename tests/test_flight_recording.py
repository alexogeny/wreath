from __future__ import annotations

import pytest

from wreath import _flight_schema as fs
from wreath._flight_schema import CaptureDisposition as D
from wreath._flight_schema import CaptureFieldClass as FC
from wreath.recording import (
    BodyCapture,
    CaptureBudget,
    CapturePolicy,
    CompiledRedaction,
    RecordingPolicy,
    RecordingPolicyError,
    RedactionPolicy,
    compile_redaction,
)


def test_forbidden_headers_rejected_in_every_set() -> None:
    for kwarg in ("header_allowlist", "header_hash", "header_mask"):
        with pytest.raises(RecordingPolicyError):
            RedactionPolicy(**{kwarg: frozenset({"Authorization"})})
        with pytest.raises(RecordingPolicyError):
            RedactionPolicy(**{kwarg: frozenset({"Set-Cookie"})})


def test_a_header_needs_one_disposition() -> None:
    with pytest.raises(RecordingPolicyError):
        RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}),
            header_hash=frozenset({"X-Trace"}),  # same header, two dispositions
        )


def test_header_sets_are_lower_cased() -> None:
    policy = RedactionPolicy(
        header_allowlist=frozenset({"X-Trace"}),
        header_hash=frozenset({"X-Request-Id"}),
        header_mask=frozenset({"X-Client"}),
    )
    assert policy.header_allowlist == frozenset({"x-trace"})
    assert policy.header_hash == frozenset({"x-request-id"})
    assert policy.header_mask == frozenset({"x-client"})


def test_deny_by_default_compiles_to_no_headers_and_metadata_body() -> None:
    plan = compile_redaction(RedactionPolicy.deny_by_default())
    assert plan.header_rules == {}
    assert plan.header("authorization") is None
    assert plan.header("x-anything") is None
    # METADATA (the default) retains only the length.
    assert plan.body(FC.REQUEST_BODY) == (D.LENGTH, 0)
    assert plan.body(FC.RESPONSE_BODY) == (D.LENGTH, 0)


def test_compiled_dispositions_match_the_sets() -> None:
    plan = compile_redaction(
        RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}),
            header_hash=frozenset({"x-request-id"}),
            header_mask=frozenset({"user-agent"}),
        )
    )
    assert plan.header("X-Trace").disposition is D.RAW
    assert plan.header("x-request-id").disposition is D.HASHED
    assert plan.header("User-Agent").disposition is D.MASKED
    assert plan.header("cookie") is None  # never listed -> dropped


def test_descriptor_ids_are_deterministic_and_order_independent() -> None:
    a = compile_redaction(
        RedactionPolicy(header_allowlist=frozenset({"b-head", "a-head", "c-head"}))
    )
    b = compile_redaction(
        RedactionPolicy(header_allowlist=frozenset({"c-head", "a-head", "b-head"}))
    )
    ids_a = {n: a.header(n).descriptor_id for n in ("a-head", "b-head", "c-head")}
    ids_b = {n: b.header(n).descriptor_id for n in ("a-head", "b-head", "c-head")}
    assert ids_a == ids_b == {"a-head": 1, "b-head": 2, "c-head": 3}
    # No zero id (0 == none) and names reverse-map by id.
    assert a.header_names == ("a-head", "b-head", "c-head")


def test_body_modes_compile_to_dispositions() -> None:
    none = compile_redaction(RedactionPolicy(body=BodyCapture.NONE))
    assert none.body(FC.REQUEST_BODY) is None
    hashed = compile_redaction(RedactionPolicy(body=BodyCapture.HASHED))
    assert hashed.body(FC.REQUEST_BODY) == (D.HASHED, 0)
    structured = compile_redaction(
        RedactionPolicy(
            body=BodyCapture.STRUCTURED, max_body_bytes=4096, max_fields=32, max_depth=8
        )
    )
    assert structured.body(FC.REQUEST_BODY) == (D.RAW, 4096)
    # A non-body field class has no body rule.
    assert structured.body(FC.REQUEST_HEADER) is None


def test_dependency_is_deny_by_default_and_independent_of_body() -> None:
    # Dependency payloads (DB params, outbound bodies) are off unless opted in,
    # regardless of the request/response body knob.
    body_only = compile_redaction(
        RedactionPolicy(body=BodyCapture.STRUCTURED, max_body_bytes=4096, max_fields=8, max_depth=4)
    )
    assert body_only.dependency() is None
    hashed_dep = compile_redaction(RedactionPolicy(dependency=BodyCapture.HASHED))
    assert hashed_dep.dependency() == (D.HASHED, 0)
    raw_dep = compile_redaction(
        RedactionPolicy(
            dependency=BodyCapture.STRUCTURED, max_body_bytes=256, max_fields=8, max_depth=4
        )
    )
    assert raw_dep.dependency() == (D.RAW, 256)


def test_query_params_compile_narrow_and_bound_like_headers() -> None:
    policy = RedactionPolicy(query_allowlist=frozenset({"user"}), query_hash=frozenset({"token"}))
    plan = compile_redaction(policy)
    assert plan.query("user").disposition is D.RAW
    assert plan.query("token").disposition is D.HASHED
    assert plan.query("secret") is None  # deny-by-default
    # A query param lives in its own descriptor namespace, not the header one.
    assert plan.header("user") is None
    # narrow: an override keeps only the shared params, least-revealing.
    narrowed = policy.narrow(RedactionPolicy(query_allowlist=frozenset({"user"})))
    assert narrowed.query_allowlist == frozenset({"user"})
    assert narrowed.query_hash == frozenset()
    # ceiling bounds an arm: it may not capture a param the ceiling drops.
    ceiling = RecordingPolicy(capture_slabs=8, max_capture_bytes=1 << 20, redaction=policy)
    ok = CapturePolicy(
        redaction=RedactionPolicy(query_hash=frozenset({"user"})),
        budget=CaptureBudget(slabs=1, slab_bytes=4096),
    )
    over = CapturePolicy(
        redaction=RedactionPolicy(query_allowlist=frozenset({"secret"})),
        budget=CaptureBudget(slabs=1, slab_bytes=4096),
    )
    assert ceiling.permits(ok)
    assert not ceiling.permits(over)


def test_dependency_narrows_and_the_ceiling_bounds_it() -> None:
    base = RedactionPolicy(
        dependency=BodyCapture.STRUCTURED, max_body_bytes=4096, max_fields=8, max_depth=4
    )
    override = RedactionPolicy(dependency=BodyCapture.HASHED)  # less revealing
    assert base.narrow(override).dependency is BodyCapture.HASHED
    ceiling = RecordingPolicy(
        capture_slabs=8,
        max_capture_bytes=1 << 20,
        redaction=RedactionPolicy(dependency=BodyCapture.HASHED),
    )
    within = CapturePolicy(
        redaction=RedactionPolicy(dependency=BodyCapture.HASHED),
        budget=CaptureBudget(slabs=1, slab_bytes=4096),
    )
    assert ceiling.permits(within)
    # A more-revealing dependency disposition than the ceiling is refused.
    over = CapturePolicy(
        redaction=RedactionPolicy(
            dependency=BodyCapture.STRUCTURED, max_body_bytes=64, max_fields=8, max_depth=4
        ),
        budget=CaptureBudget(slabs=1, slab_bytes=4096),
    )
    assert not ceiling.permits(over)


def test_narrow_intersects_and_takes_the_least_revealing() -> None:
    base = RedactionPolicy(
        header_allowlist=frozenset({"x-a", "x-b"}),
        header_hash=frozenset({"x-h"}),
        body=BodyCapture.STRUCTURED,
        max_body_bytes=8192,
        max_fields=32,
        max_depth=8,
    )
    override = RedactionPolicy(
        header_allowlist=frozenset({"x-b", "x-c"}),  # x-a not allowed by the layer
        body=BodyCapture.HASHED,  # less revealing than STRUCTURED
        max_body_bytes=1024,
    )
    narrowed = base.narrow(override)
    assert narrowed.header_allowlist == frozenset({"x-b"})
    assert narrowed.header_hash == frozenset()  # override didn't hash x-h
    assert narrowed.body is BodyCapture.HASHED
    assert narrowed.max_body_bytes == 1024


def test_recording_ceiling_bounds_hashed_and_masked_arms() -> None:
    ceiling = RecordingPolicy(
        capture_slabs=8,
        max_capture_bytes=1 << 20,
        redaction=RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}),
            header_hash=frozenset({"x-id"}),
        ),
    )
    # A masked arm of a header the ceiling would reveal more of is permitted.
    ok = CapturePolicy(
        redaction=RedactionPolicy(header_mask=frozenset({"x-trace"})),
        budget=CaptureBudget(slabs=1, slab_bytes=4096),
    )
    assert ceiling.permits(ok)
    # An arm capturing a header the ceiling drops entirely is refused.
    bad = CapturePolicy(
        redaction=RedactionPolicy(header_allowlist=frozenset({"x-secret-ish"})),
        budget=CaptureBudget(slabs=1, slab_bytes=4096),
    )
    assert not ceiling.permits(bad)


def test_hashing_and_masking_a_header_the_ceiling_drops_are_both_refused() -> None:
    ceiling = RecordingPolicy(
        capture_slabs=8,
        max_capture_bytes=1 << 20,
        redaction=RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}),
            header_hash=frozenset({"x-id"}),
        ),
    )

    def arm(**redaction) -> CapturePolicy:
        return CapturePolicy(
            redaction=RedactionPolicy(**redaction),
            budget=CaptureBudget(slabs=1, slab_bytes=4096),
        )

    # Hashing or masking a name the ceiling never mentions: both refused. The
    # name is an ordinary one -- `authorization` is refused a step earlier, by
    # the never-capturable list, which would answer for this test instead.
    assert not ceiling.permits(arm(header_hash=frozenset({"x-secret-ish"})))
    assert not ceiling.permits(arm(header_mask=frozenset({"x-secret-ish"})))
    # ... and the same two dispositions over a name the ceiling *does* reveal
    # more of are permitted, so neither refusal is a blanket one.
    assert ceiling.permits(arm(header_hash=frozenset({"x-trace"})))
    assert ceiling.permits(arm(header_mask=frozenset({"x-id"})))


def test_the_query_sets_are_bounded_by_the_ceiling_as_well_as_the_headers() -> None:
    ceiling = RecordingPolicy(
        capture_slabs=8,
        max_capture_bytes=1 << 20,
        redaction=RedactionPolicy(
            query_allowlist=frozenset({"page"}),
            query_hash=frozenset({"user"}),
        ),
    )

    def arm(**redaction) -> CapturePolicy:
        return CapturePolicy(
            redaction=RedactionPolicy(**redaction),
            budget=CaptureBudget(slabs=1, slab_bytes=4096),
        )

    assert ceiling.permits(arm(query_allowlist=frozenset({"page"})))
    assert ceiling.permits(arm(query_hash=frozenset({"page"})))
    assert ceiling.permits(arm(query_mask=frozenset({"user"})))
    assert not ceiling.permits(arm(query_allowlist=frozenset({"token"})))
    assert not ceiling.permits(arm(query_hash=frozenset({"token"})))
    assert not ceiling.permits(arm(query_mask=frozenset({"token"})))
    # An allowlisted arm over a name the ceiling only hashes reveals more, too.
    assert not ceiling.permits(arm(query_allowlist=frozenset({"user"})))


_flight = pytest.importorskip("wreath._native._flight")
KEY = (0xABCDEF0123456789, 0x0F1E2D3C4B5A6978)


def _apply_headers(req: object, plan: CompiledRedaction, headers: dict) -> None:
    """Model the request-path seam: consult the plan, capture only what it
    permits, with the disposition the plan chose."""
    for name, value in headers.items():
        rule = plan.header(name)
        if rule is not None:
            req.capture(int(FC.REQUEST_HEADER), rule.descriptor_id, int(rule.disposition), value)


def test_compiled_plan_drives_native_capture_deny_by_default() -> None:
    plan = compile_redaction(
        RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}),
            header_hash=frozenset({"x-request-id"}),
            body=BodyCapture.HASHED,
        )
    )
    rec = _flight.Recorder(
        _flight.MODE_FORENSIC,
        ring_records=256,
        active_requests=16,
        capture_slabs=4,
        slab_bytes=4096,
        detailed_sample_rate=1.0,
        capture_hash_key=KEY,
    )
    headers = {
        "authorization": b"Bearer super-secret-token",  # forbidden -> dropped
        "cookie": b"session=deadbeef",  # forbidden -> dropped
        "x-trace": b"abc123",  # allowlisted -> RAW
        "x-request-id": b"req-9f8e7d",  # hashed
        "user-agent": b"curl/8.0",  # unlisted -> dropped
    }
    body = b'{"password":"hunter2"}'
    req = rec.begin(protocol=_flight.PROTO_HTTP1, start_ns=0)
    _apply_headers(req, plan, headers)
    disp, _limit = plan.body(FC.REQUEST_BODY)
    req.capture(int(FC.REQUEST_BODY), 0, int(disp), body)
    req.finish(now_ns=1000, status=200)

    (slab,) = rec.drain_captures()
    # No secret plaintext ever reached the slab.
    for secret in (b"super-secret-token", b"session=deadbeef", b"hunter2", b"req-9f8e7d"):
        assert secret not in slab
    decoded = fs.CaptureSlab.decode(slab)
    by_id = {f.descriptor_id: f for f in decoded.fields if f.field_class is FC.REQUEST_HEADER}
    # Exactly the two listed headers were captured (deny-by-default dropped 3).
    assert set(by_id) == {
        plan.header("x-trace").descriptor_id,
        plan.header("x-request-id").descriptor_id,
    }
    assert by_id[plan.header("x-trace").descriptor_id].payload == b"abc123"  # RAW
    hashed = by_id[plan.header("x-request-id").descriptor_id]
    assert hashed.disposition is D.HASHED and len(hashed.payload) == 8
    # The body was hashed (metadata mode would be length-only).
    body_field = next(f for f in decoded.fields if f.field_class is FC.REQUEST_BODY)
    assert body_field.disposition is D.HASHED
    assert body_field.original_length == len(body)
