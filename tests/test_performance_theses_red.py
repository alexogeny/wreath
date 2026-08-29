from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.thesis

_ROOT = Path(__file__).parents[1]
_SRC = _ROOT / "src" / "wreath"
_NATIVE = _SRC / "_native"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at + len(start))
    return source[start_at:end_at]


def test_direct_native_response_uses_typed_abi_not_message_dict() -> None:
    app = _text(_SRC / "app.py")
    server = _text(_NATIVE / "server_http1.c")

    assert "._wreath_response(" in app
    assert '"_wreath_response"' in server


def test_native_request_parser_does_not_materialize_header_list_before_activation() -> None:
    source = _text(_NATIVE / "http.c")
    parser = _between(
        source,
        "wreath_http_parse_request_parts(",
        "wreath_http_parse_request(PyObject",
    )

    assert "PyList_New" not in parser
    assert "PyTuple_New" not in parser


def test_builtin_pre_activation_stack_has_one_compiled_native_phase() -> None:
    baseline = json.loads(_text(_ROOT / "tools" / "baselines" / "request-boundary-baseline.json"))
    realistic = baseline["scenarios"]["realistic"]

    # Leaves room for app entry, one credential-verifier activation, and route
    # activation while rejecting the current predicate-by-predicate sequencing.
    assert realistic["pre_activation"]["python"] <= 10


@pytest.mark.parametrize("source_name", ["server_http2.c", "http3_asgi.c"])
def test_every_native_http_protocol_uses_lazy_request_context(source_name: str) -> None:
    source = _text(_NATIVE / source_name)

    assert "wreath_request_context_new" in source


def test_outbound_client_has_native_incremental_protocol_not_only_codecs() -> None:
    protocol_source = _NATIVE / "client_http1.c"
    module = _text(_NATIVE / "_clientmodule.c")

    assert protocol_source.exists()
    assert "wreath_register_http_client_protocol" in module


def test_file_response_has_private_descriptor_transport_path() -> None:
    response = _text(_SRC / "response.py")
    server = _text(_NATIVE / "server_http1.c")

    assert "asyncio.to_thread(_reader)" not in response
    assert "wreath.file" in server


@pytest.mark.parametrize(
    ("blocking_step", "compiled_step"),
    [
        ("payload = await request.json()", "decode_json_validation_tape"),
        ("form = await request.form()", "decode_multipart_validation_tape"),
    ],
)
def test_typed_body_binding_fuses_decode_and_validation(
    blocking_step: str, compiled_step: str
) -> None:
    binding = _text(_SRC / "binding.py")

    assert blocking_step not in binding
    assert compiled_step in binding


def test_routes_compile_to_one_canonical_application_image() -> None:
    app = _text(_SRC / "app.py")
    openapi = _text(_SRC / "openapi.py")
    typegen = _text(_SRC / "typegen" / "inspect.py")

    assert "_application_image" in app
    assert "inspect_handler(" not in openapi
    assert "inspect_handler(" not in typegen


def test_static_mounts_use_compiled_prefix_matcher_on_route_miss() -> None:
    app = _text(_SRC / "app.py")

    assert "for mount_prefix, static_handler in self._static_mounts" not in app
    assert "_static_match" in app


def test_cached_orm_plan_owns_bind_extraction_program() -> None:
    compiler = _text(_SRC / "orm" / "compiler.py")
    cached_plan = _between(compiler, "class _CachedPlan:", "def quote(")

    assert "bind_program" in cached_plan
    assert "_collect_binds(select)" not in compiler


def test_shared_native_scheduler_owns_bounded_waiters_and_deadlines() -> None:
    scheduler = _NATIVE / "scheduler.c"
    core_module = _text(_NATIVE / "_coremodule.c")

    assert scheduler.exists()
    assert "wreath_register_scheduler" in core_module


@pytest.mark.parametrize(
    ("relative_path", "cooperative_boundary"),
    [
        ("json.c", "json_token_tape"),
        ("multipart.c", "multipart_span_tape"),
        ("templates.c", "template_render_chunks"),
        ("postgres/hydrate.c", "hydrate_batch_budget"),
    ],
)
def test_large_native_object_builders_have_cooperative_boundaries(
    relative_path: str, cooperative_boundary: str
) -> None:
    source = _text(_NATIVE / relative_path)

    assert cooperative_boundary in source
