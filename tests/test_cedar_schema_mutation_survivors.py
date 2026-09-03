from __future__ import annotations

import pytest

from wreath._auth.cedar_schema import (
    CedarSchema,
    _parts,
    _Record,
    _Set,
    _statements,
    _type,
    validate_context_expression,
)


def test_part_parser_keeps_quoted_escaped_and_nested_commas_together() -> None:
    assert _parts('a, "x,y", c') == ("a", '"x,y"', "c")
    assert _parts('"a\\\",b", c') == ('"a\\\",b"', "c")
    assert _parts("outer: {left: String, right: Long}, tail: Bool") == (
        "outer: {left: String, right: Long}",
        "tail: Bool",
    )


def test_statement_parser_keeps_nested_and_escaped_semicolons_together() -> None:
    source = 'type R = {"quoted\\";name": String, nested: Set<{x: Long}>}; action "a";'
    assert _statements(source) == (
        'type R = {"quoted\\";name": String, nested: Set<{x: Long}>}',
        'action "a"',
    )


def test_statement_parser_ignores_empty_statements() -> None:
    assert _statements(' ; action "read"; ;') == ('action "read"',)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ('action "read"', "missing ';'"),
        ("type R = {x: String;", "missing ';'"),
        ('action "read;', "missing ';'"),
    ],
)
def test_statement_parser_refuses_each_unterminated_shape(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _statements(source)


def test_type_parser_handles_quoted_fields_records_sets_and_aliases() -> None:
    parsed = _type('{"display name": Set<Name>, plain: Long}', {"Name": "String"})
    assert parsed == _Record(
        {
            "display name": _Set("String"),
            "plain": "Long",
        }
    )


@pytest.mark.parametrize("source", ["{x: String", "x: String}", "Set<String", "String>"])
def test_type_parser_requires_both_delimiters(source: str) -> None:
    assert _type(source, {}) == source


def test_type_parser_refuses_invalid_fields_and_excessive_nesting() -> None:
    with pytest.raises(ValueError, match="invalid Cedar schema field"):
        _type("{not-a-field}", {})
    nested = "String"
    for _ in range(66):
        nested = f"Set<{nested}>"
    with pytest.raises(ValueError, match="nesting exceeds"):
        _type(nested, {})


@pytest.mark.parametrize("source", [None, 3, "", "   "])
def test_schema_refuses_non_text_and_blank_sources(source) -> None:
    with pytest.raises(ValueError, match="non-empty text"):
        CedarSchema(source)


def test_schema_refuses_oversized_utf8_before_parsing() -> None:
    with pytest.raises(ValueError, match="1048576 UTF-8 bytes"):
        CedarSchema("é" * 524_289)


def test_schema_refuses_namespaces_and_unsupported_declarations() -> None:
    with pytest.raises(ValueError, match="namespace blocks are not supported"):
        CedarSchema("namespace Shop { entity User; };")
    with pytest.raises(ValueError, match="unsupported Cedar schema declaration"):
        CedarSchema("common String;")


def test_schema_parses_empty_and_explicit_entity_records() -> None:
    schema = CedarSchema('entity Empty; entity User = {name: String}; action "read";')
    assert schema._entities == {
        "Empty": _Record({}),
        "User": _Record({"name": "String"}),
    }


def test_schema_refuses_an_entity_body_that_is_not_a_record() -> None:
    with pytest.raises(ValueError, match="entity 'User' must declare a record"):
        CedarSchema("entity User = String;")


def test_schema_action_parser_ignores_non_context_entries_and_reads_context() -> None:
    schema = CedarSchema(
        "type Context = {tenant: String}; "
        'action "read" appliesTo {principal: User, resource: Doc, context: Context};'
    )
    assert schema.contexts({"read"}) == (_Record({"tenant": "String"}),)


def test_schema_context_requires_a_separator_and_exact_context_key() -> None:
    schema = CedarSchema(
        'action "read" appliesTo {contextual: {wrong: String}, context {bad: String}, context};'
    )
    assert schema.contexts({"read"}) == (None,)


def test_schema_refuses_an_unknown_action_parent() -> None:
    with pytest.raises(ValueError, match="unknown parent 'missing'"):
        CedarSchema('action "child" in [Action::"missing"];')


def test_schema_accepts_declared_parents_and_computes_descendants() -> None:
    schema = CedarSchema('action "group"; action "child" in [Action::"group"];')
    assert schema.action_parents("child") == ("group",)
    assert schema.descendants("group") == frozenset({"group", "child"})


def test_guarded_context_access_requires_text_and_a_declared_context() -> None:
    with pytest.raises(ValueError, match="attribute name must be text"):
        validate_context_expression((10, (1, 3), 3), ())
    with pytest.raises(ValueError, match="declare no context"):
        validate_context_expression((10, (1, 3), "tenant"), ())


def test_guarded_context_access_checks_every_applicable_record() -> None:
    complete = (_Record({"tenant": "String"}), _Record({"tenant": "String"}))
    validate_context_expression((10, (1, 3), "tenant"), complete)
    with pytest.raises(ValueError, match="unknown Cedar schema attribute context.tenant"):
        validate_context_expression(
            (10, (1, 3), "tenant"),
            (_Record({"tenant": "String"}), _Record({"other": "String"})),
        )


def test_nested_guarded_access_uses_the_nested_record_and_ignores_unknown_bases() -> None:
    contexts = (_Record({"profile": _Record({"name": "String"})}),)
    validate_context_expression((10, (16, (1, 3), "profile"), "name"), contexts)
    validate_context_expression((10, (1, 0), "unknown"), contexts)


def test_guarded_access_opcode_requires_a_three_item_tuple() -> None:
    contexts = (_Record({}),)
    validate_context_expression([10, (1, 3), "missing"], contexts)
    validate_context_expression((10, (1, 3)), contexts)


def test_read_context_access_requires_text_context_and_known_fields() -> None:
    with pytest.raises(ValueError, match="attribute name must be text"):
        validate_context_expression((16, (1, 3), object()), ())
    with pytest.raises(ValueError, match="declare no context"):
        validate_context_expression((16, (1, 3), "tenant"), ())
    with pytest.raises(ValueError, match="unknown Cedar schema attribute context.tenant"):
        validate_context_expression((16, (1, 3), "tenant"), (_Record({}),))
    validate_context_expression(
        (16, (1, 3), "tenant"),
        (_Record({"tenant": "String"}),),
    )


def test_read_access_opcode_requires_a_three_item_tuple() -> None:
    contexts = (_Record({}),)
    validate_context_expression([16, (1, 3), "missing"], contexts)
    validate_context_expression((16, (1, 3)), contexts)


def test_nested_read_access_tracks_the_complete_attribute_path() -> None:
    contexts = (_Record({"profile": _Record({"name": "String"})}),)
    validate_context_expression((16, (16, (1, 3), "profile"), "name"), contexts)
    with pytest.raises(ValueError, match="context.profile.missing"):
        validate_context_expression((16, (16, (1, 3), "profile"), "missing"), contexts)
    validate_context_expression((16, (1, 0), "unknown"), contexts)
