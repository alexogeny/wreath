from __future__ import annotations

from wreath.typegen.typescript_renderer import (
    GENERATOR_HEADER,
    _escape,
    _method_lines,
    _property_key,
    _ts_literal,
    render_client,
    render_models,
    ts_type,
)

STRING = ("string", None, (), ())
NUMBER = ("number", None, (), ())
UNKNOWN = ("unknown", None, (), ())


def test_typescript_literal_renders_none() -> None:
    assert _ts_literal(None) == "null"


def test_typescript_literal_renders_true() -> None:
    assert _ts_literal(True) == "true"


def test_typescript_literal_renders_false() -> None:
    assert _ts_literal(False) == "false"


def test_typescript_literal_quotes_strings() -> None:
    assert _ts_literal('say "hello"') == '"say \\"hello\\""'


def test_typescript_literal_renders_integers() -> None:
    assert _ts_literal(7) == "7"


def test_typescript_literal_renders_floats() -> None:
    assert _ts_literal(1.5) == "1.5"


def test_typescript_escape_quotes_backslashes() -> None:
    assert _escape("\\") == "\\\\"


def test_typescript_escape_quotes_double_quotes() -> None:
    assert _escape('"') == '\\"'


def test_typescript_escape_quotes_newlines() -> None:
    assert _escape("\n") == "\\n"


def test_typescript_escape_quotes_carriage_returns() -> None:
    assert _escape("\r") == "\\r"


def test_typescript_escape_quotes_tabs() -> None:
    assert _escape("\t") == "\\t"


def test_typescript_escape_quotes_other_control_characters() -> None:
    assert _escape("\x01") == "\\u0001"


def test_empty_array_type_has_an_unknown_element() -> None:
    assert ts_type(("array", None, (), ())) == "unknown[]"


def test_union_array_type_keeps_precedence_with_parentheses() -> None:
    union = ("union", None, (STRING, NUMBER), ())

    assert ts_type(("array", None, (union,), ())) == "(string | number)[]"


def test_empty_record_type_has_an_unknown_value() -> None:
    assert ts_type(("record", None, (), ())) == "Record<string, unknown>"


def test_page_type_preserves_its_item_type() -> None:
    assert ts_type(("page", None, (STRING,), ())) == (
        "{ items: readonly string[]; total: number; page: number; size: number }"
    )


def test_non_identifier_property_is_quoted() -> None:
    assert _property_key("display-name") == '"display-name"'


def test_empty_model_module_has_no_trailing_block_newline() -> None:
    assert render_models(()) == GENERATOR_HEADER.encode()


def test_path_substitution_uses_only_the_path_parameter() -> None:
    params = (
        ("pathId", "id", "path", STRING, True),
        ("queryId", "id", "query", STRING, False),
    )
    operation = ("getItem", "GET", "/items/{id}", params, None, None, STRING)
    source = "\n".join(_method_lines(operation))

    assert "parameters.pathId" in source
    assert "encodeURIComponent(String(parameters.queryId))" not in source


def test_header_only_method_still_declares_a_parameters_object() -> None:
    params = (("traceId", "x-trace-id", "header", STRING, False),)
    operation = ("getItem", "GET", "/items", params, None, None, STRING)

    assert _method_lines(operation)[0] == (
        "    async getItem(parameters: GetItemParameters, init?: RequestInit): "
        "Promise<string> {"
    )


def test_optional_header_assignment_is_guarded() -> None:
    params = (("traceId", "x-trace-id", "header", STRING, False),)
    operation = ("getItem", "GET", "/items", params, None, None, STRING)
    source = "\n".join(_method_lines(operation))

    assert "if (parameters.traceId !== undefined) headers.set" in source


def test_required_header_assignment_is_unconditional() -> None:
    params = (("traceId", "x-trace-id", "header", STRING, True),)
    operation = ("getItem", "GET", "/items", params, None, None, STRING)
    source = "\n".join(_method_lines(operation))

    assert "if (parameters.traceId !== undefined)" not in source
    assert 'headers.set("x-trace-id", String(parameters.traceId));' in source


def test_empty_client_model_import_set_emits_no_import() -> None:
    source = render_client(((), ())).decode()

    assert 'from "./models"' not in source
