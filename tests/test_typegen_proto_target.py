from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import pytest

from wreath import Wreath
from wreath.pagination import Page
from wreath.temporal import Instant
from wreath.typegen.cli import TypegenCliError, TypegenOptions, run
from wreath.typegen.inspect import build_api_model
from wreath.typegen.targets.proto import ProtoTargetError, render_proto


@dataclass
class Llama:
    id: int
    name: str
    weight: float
    healthy: bool
    paddock: str | None = None


def _rendered(app: Wreath) -> str:
    return render_proto(build_api_model(app))["api.proto"]


def _llama_app() -> Wreath:
    app = Wreath()

    @app.get("/llamas/{llama_id}")
    async def get_llama(request: Any, llama_id: int) -> Llama:
        return Llama(id=llama_id, name="Bo", weight=90.5, healthy=True)

    return app


def test_the_target_emits_one_proto_file() -> None:
    assert set(render_proto(build_api_model(_llama_app()))) == {"api.proto"}


def test_scalars_map_to_proto3_types() -> None:
    source = _rendered(_llama_app())
    assert "int64 id = 1;" in source, source
    assert "string name = 2;" in source, source
    assert "double weight = 3;" in source, source
    assert "bool healthy = 4;" in source, source


def test_an_optional_field_is_spelled_optional() -> None:
    assert "optional string paddock = 5;" in _rendered(_llama_app())


def test_the_header_declares_proto3_and_a_package() -> None:
    source = _rendered(_llama_app())
    assert 'syntax = "proto3";' in source
    assert "package wreath;" in source, source


def test_the_header_warns_that_field_numbers_track_declaration_order() -> None:
    source = _rendered(_llama_app())
    assert "FIELD NUMBERS COME FROM DECLARATION ORDER" in source


def test_no_service_block_is_generated() -> None:
    assert "service " not in _rendered(_llama_app())


@dataclass
class Tagged:
    at: Instant
    ident: UUID
    amount: Decimal
    blob: bytes


def _tagged_app() -> Wreath:
    app = Wreath()

    @app.get("/tagged")
    async def get_tagged(request: Any) -> Tagged:
        raise NotImplementedError

    return app


def test_named_scalars_travel_as_string_except_bytes() -> None:
    source = _rendered(_tagged_app())
    assert "string at = 1;" in source, source
    assert "string ident = 2;" in source, source
    assert "string amount = 3;" in source, source
    assert "bytes blob = 4;" in source, source


@dataclass
class Herd:
    names: list[str]
    counts: dict[str, int]
    grade: Literal["a", "b"]


def _herd_app() -> Wreath:
    app = Wreath()

    @app.get("/herds")
    async def get_herd(request: Any) -> Herd:
        raise NotImplementedError

    return app


def test_a_list_becomes_repeated() -> None:
    assert "repeated string names = 1;" in _rendered(_herd_app())


def test_a_mapping_becomes_a_map() -> None:
    assert "map<string, int64> counts = 2;" in _rendered(_herd_app())


def test_a_literal_travels_as_its_underlying_scalar() -> None:
    assert "string grade = 3;" in _rendered(_herd_app())


def _paged_app() -> Wreath:
    app = Wreath()

    @app.get("/llamas")
    async def list_llamas(request: Any) -> Page[Llama]:
        raise NotImplementedError

    return app


def test_a_page_becomes_a_named_wrapper_message() -> None:
    source = _rendered(_paged_app())
    assert "message PageLlama {" in source, source
    assert "repeated Llama items = 1;" in source, source
    assert "int64 total = 2;" in source, source


def test_no_page_wrapper_when_nothing_returns_one() -> None:
    assert "message Page" not in _rendered(_llama_app())


def test_a_bare_page_never_reaches_the_proto_target_at_all() -> None:
    from wreath.typegen.model import TypegenError

    app = Wreath()

    @app.get("/pages")
    async def list_pages(request: Any) -> Page:
        raise NotImplementedError

    with pytest.raises(TypegenError) as caught:
        _rendered(app)
    assert "unsupported annotation" in str(caught.value), caught.value
    assert "getPages" in str(caught.value), caught.value


@dataclass
class Untyped:
    anything: Any


def _untyped_app() -> Wreath:
    app = Wreath()

    @app.get("/untyped")
    async def get_untyped(request: Any) -> Untyped:
        raise NotImplementedError

    return app


def test_an_unannotated_value_is_refused_naming_the_field() -> None:
    with pytest.raises(ProtoTargetError) as caught:
        _rendered(_untyped_app())
    message = str(caught.value)
    assert "an unannotated value" in message, message
    assert "Untyped.anything" in message, message


@dataclass
class Mixed:
    either: int | str


def _mixed_app() -> Wreath:
    app = Wreath()

    @app.get("/mixed")
    async def get_mixed(request: Any) -> Mixed:
        raise NotImplementedError

    return app


def test_a_multi_type_union_is_refused_naming_oneof() -> None:
    with pytest.raises(ProtoTargetError) as caught:
        _rendered(_mixed_app())
    message = str(caught.value)
    assert "a multi-type union" in message, message
    assert "oneof" in message, message
    assert "Mixed.either" in message, message


@dataclass
class Nested:
    rows: list[list[str]]


def _nested_app() -> Wreath:
    app = Wreath()

    @app.get("/nested")
    async def get_nested(request: Any) -> Nested:
        raise NotImplementedError

    return app


def test_a_repeated_repeated_value_is_refused() -> None:
    with pytest.raises(ProtoTargetError) as caught:
        _rendered(_nested_app())
    assert "a repeated repeated value" in str(caught.value), caught.value


@dataclass
class Pair:
    both: tuple[int, str]


def _tuple_app() -> Wreath:
    app = Wreath()

    @app.get("/pair")
    async def get_pair(request: Any) -> Pair:
        raise NotImplementedError

    return app


def test_a_heterogeneous_tuple_is_refused() -> None:
    with pytest.raises(ProtoTargetError) as caught:
        _rendered(_tuple_app())
    assert "a heterogeneous tuple" in str(caught.value), caught.value


@dataclass
class Flags:
    on: Literal[True, False]


@dataclass
class Codes:
    code: Literal[1, 2, 3]


@dataclass
class Nullable:
    grade: Literal["a", None]


def _flags_app() -> Wreath:
    app = Wreath()

    @app.get("/flags")
    async def get_flags(request: Any) -> Flags:
        raise NotImplementedError

    return app


def _codes_app() -> Wreath:
    app = Wreath()

    @app.get("/codes")
    async def get_codes(request: Any) -> Codes:
        raise NotImplementedError

    return app


def _nullable_app() -> Wreath:
    app = Wreath()

    @app.get("/nullable")
    async def get_nullable(request: Any) -> Nullable:
        raise NotImplementedError

    return app


def test_a_boolean_literal_becomes_bool() -> None:
    assert "bool on = 1;" in _rendered(_flags_app())


def test_an_integer_literal_becomes_int64() -> None:
    assert "int64 code = 1;" in _rendered(_codes_app())


@dataclass
class MixedLiteral:
    odd: Literal["a", 1]


def _mixed_literal_app() -> Wreath:
    app = Wreath()

    @app.get("/odd")
    async def get_odd(request: Any) -> MixedLiteral:
        raise NotImplementedError

    return app


def test_a_literal_mixing_value_types_is_refused() -> None:
    with pytest.raises(ProtoTargetError) as caught:
        _rendered(_mixed_literal_app())
    assert "a literal mixing value types" in str(caught.value), caught.value


@dataclass
class Shelf:
    page: Page[Llama]


def _page_field_app() -> Wreath:
    app = Wreath()

    @app.get("/shelf")
    async def get_shelf(request: Any) -> Shelf:
        raise NotImplementedError

    return app


def test_a_page_as_a_model_field_renders_the_wrapper() -> None:
    source = _rendered(_page_field_app())
    assert "PageLlama page = 1;" in source, source


def test_a_literal_ignores_its_none_member_when_choosing_a_type() -> None:
    source = _rendered(_nullable_app())
    assert "string grade = 1;" in source, source


def test_a_refusal_reaches_the_cli_as_an_error_not_a_file(tmp_path) -> None:
    options = TypegenOptions(target="proto", output=str(tmp_path))
    with pytest.raises(TypegenCliError):
        run(_untyped_app(), options)
    assert not (tmp_path / "api.proto").exists(), "a refused schema must not be written"


def test_the_cli_selects_the_proto_target(tmp_path) -> None:
    assert run(_llama_app(), TypegenOptions(target="proto", output=str(tmp_path))) == 0
    assert (tmp_path / "api.proto").exists()


def test_the_other_targets_are_unaffected(tmp_path) -> None:
    from wreath.typegen.targets.typescript import render_typescript

    api = build_api_model(_llama_app())
    before = render_typescript(api)
    render_proto(api)
    assert render_typescript(api) == before


GOLDEN = """\
// Generated by wreath typegen. Do not edit.
//
// Message shapes for Wreath 0.1.0.
//
// FIELD NUMBERS COME FROM DECLARATION ORDER. Reordering a field in
// the source dataclass renumbers it here, which is a wire-breaking
// change that no test on either side will notice. Pin this file in
// review, or declare the message with `wreath.protobuf`, where field
// numbers are explicit and a reorder cannot move them.
//
// Messages only: `service` blocks are not generated. See the module
// docstring in `wreath/typegen/targets/proto.py` for why.

syntax = "proto3";

package wreath;

message Llama {
  int64 id = 1;
  string name = 2;
  double weight = 3;
  bool healthy = 4;
  optional string paddock = 5;
}
"""


def test_the_emitted_schema_matches_the_golden() -> None:
    assert _rendered(_llama_app()) == GOLDEN
