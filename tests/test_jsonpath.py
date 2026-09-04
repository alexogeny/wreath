from __future__ import annotations

import pytest

from wreath._json import jsonpath_find as native_jsonpath_find
from wreath.jsonpath import (
    JSONPathError,
    _IRegexp,
    _iregexp_fullmatch,
    _IRegexpError,
    compile_jsonpath,
    jsonpath,
)

STORE = {
    "store": {
        "book": [
            {"category": "reference", "author": "Nigel Rees", "price": 8.95},
            {"category": "fiction", "author": "Evelyn Waugh", "price": 12.99},
            {
                "category": "fiction",
                "author": "Herman Melville",
                "isbn": "0-553",
                "price": 8.99,
            },
            {
                "category": "fiction",
                "author": "J. R. R. Tolkien",
                "isbn": "0-395",
                "price": 22.99,
            },
        ],
        "bicycle": {"color": "red", "price": 399},
    }
}


def test_root_and_child_segments_return_an_ordered_nodelist() -> None:
    matches = compile_jsonpath("$.store.book[*].author").find(STORE)
    assert [match.value for match in matches] == [
        "Nigel Rees",
        "Evelyn Waugh",
        "Herman Melville",
        "J. R. R. Tolkien",
    ]
    assert matches[0].path == "$['store']['book'][0]['author']"


def test_descendant_segment_visits_nested_objects_in_preorder() -> None:
    assert jsonpath("$..author", STORE) == [
        "Nigel Rees",
        "Evelyn Waugh",
        "Herman Melville",
        "J. R. R. Tolkien",
    ]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("$.store.book[0,2].author", ["Nigel Rees", "Herman Melville"]),
        ("$.store.book[-1].author", ["J. R. R. Tolkien"]),
        ("$.store.book[1:4:2].author", ["Evelyn Waugh", "J. R. R. Tolkien"]),
        ("$.store.book[::-1].author", [
            "J. R. R. Tolkien",
            "Herman Melville",
            "Evelyn Waugh",
            "Nigel Rees",
        ]),
        ("$.store.book[0:2:0].author", []),
    ],
)
def test_index_union_and_slice_selectors(expression, expected) -> None:
    assert jsonpath(expression, STORE) == expected


def test_filter_selectors_support_existence_comparison_and_boolean_logic() -> None:
    assert jsonpath("$.store.book[?@.isbn].author", STORE) == [
        "Herman Melville",
        "J. R. R. Tolkien",
    ]
    assert jsonpath("$.store.book[?@.price < 10 && !(@.category == 'fiction')].author", STORE) == [
        "Nigel Rees"
    ]


def test_filter_functions_cover_length_count_value_match_and_search() -> None:
    document = {"items": [{"name": "alpha", "tags": ["a", "b"]}, {"name": "beta"}]}
    assert jsonpath("$.items[?length(@.name) == 5].name", document) == ["alpha"]
    assert jsonpath("$.items[?count(@.tags[*]) == 2].name", document) == ["alpha"]
    assert jsonpath("$.items[?value(@.name) == 'beta'].name", document) == ["beta"]
    assert jsonpath("$.items[?match(@.name, 'a.*')].name", document) == ["alpha"]
    assert jsonpath("$.items[?search(@.name, 'et')].name", document) == ["beta"]


def test_string_names_and_normalized_paths_escape_special_characters() -> None:
    match = compile_jsonpath("$['a\\'b']").find({"a'b": 1})[0]
    assert match.value == 1
    assert match.path == "$['a\\'b']"


def test_string_literals_follow_rfc_9535_quoting_and_normalization() -> None:
    document = {'a"b': 1, "\b\t\n\f\r\v": 2, "🁁": 3}
    assert jsonpath("$['a\"b']", document) == [1]
    assert jsonpath('$["a\\"b"]', document) == [1]
    control = compile_jsonpath('$["\\b\\t\\n\\f\\r\\u000b"]').find(document)[0]
    assert control.path == "$['\\b\\t\\n\\f\\r\\u000b']"
    assert jsonpath("$['\\uD83C\\uDC41']", document) == [3]


def test_whitespace_may_precede_each_segment() -> None:
    assert jsonpath("$ .store ['book'] [0] .author", STORE) == ["Nigel Rees"]
    assert jsonpath("$.store.book[1 : 4 : 2].author", STORE) == [
        "Evelyn Waugh",
        "J. R. R. Tolkien",
    ]


def test_match_and_search_use_rfc_9485_iregexp_semantics() -> None:
    values = ["ж", "Ж", "жЖ", "\r", "\n", "\u2028", "abc", "abcx", "ABC", "123"]
    assert jsonpath("$[?match(@, '\\\\p{Lu}') ]", values) == ["Ж"]
    assert jsonpath("$[?search(@, '\\\\p{Lu}') ]", values) == ["Ж", "жЖ", "ABC"]
    assert jsonpath("$[?match(@, '(ab)c') ]", values) == ["abc"]
    assert jsonpath("$[?match(@, '[A-Z]+') ]", values) == ["ABC"]
    assert jsonpath("$[?match(@, '\\\\p{L}+') ]", values) == ["ж", "Ж", "жЖ", "abc", "abcx", "ABC"]
    assert jsonpath("$[?match(@, '\\\\P{L}+') ]", values) == ["\r", "\n", "\u2028", "123"]
    assert jsonpath("$[?match(@, '[\\\\p{Lu}]') ]", values) == ["Ж"]
    assert jsonpath("$[?match(@, '.') ]", values) == ["ж", "Ж", "\u2028"]
    assert jsonpath("$[?match(@, '^ab.*') ]", values) == ["abc", "abcx"]
    assert jsonpath("$[?match(@, '.*bc$') ]", values) == ["abc"]


@pytest.mark.parametrize("literal", [",", "0", "A", "~", "💡"])
def test_iregexp_literal_ranges_are_accepted(literal: str) -> None:
    assert jsonpath(f"$[?match(@, '{literal}') ]", [literal]) == [literal]


def test_iregexp_refuses_invalid_atoms_and_escapes_without_raising() -> None:
    assert jsonpath("$[?match(@, ']') ]", ["]"]) == []
    assert jsonpath("$[?match(@, '\\\\') ]", ["x"]) == []
    assert jsonpath("$[?match(@, '\\\\p{Lu') ]", ["A"]) == []
    assert jsonpath("$[?match(@, '\\\\.') ]", ["."]) == ["."]


def test_iregexp_refuses_a_truncated_character_class_atom() -> None:
    with pytest.raises(_IRegexpError):
        _IRegexp("", "a").class_atom()


@pytest.mark.parametrize(
    ("pattern", "value", "matches"),
    [
        ("[^a]", "b", True),
        ("[^a]", "a", False),
        ("[-a]", "-", True),
        ("[a-]", "-", True),
        ("[a-z]", "m", True),
        ("[a-z]", "A", False),
        ("[\\p{Lu}]", "Ж", True),
        ("[\\P{Lu}]", "ж", True),
    ],
)
def test_iregexp_character_class_forms(
    pattern: str,
    value: str,
    matches: bool,
) -> None:
    assert (_iregexp_fullmatch(pattern, value) is not None) is matches


@pytest.mark.parametrize(
    "pattern",
    ["(", "[", "[]", "[a", "[a-\\p{L}]", "[[]", "\\q", "\\p{NoSuch}", "\\p{L"],
)
def test_iregexp_rejects_each_malformed_atom_or_class(pattern: str) -> None:
    assert _iregexp_fullmatch(pattern, "a") is None


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("$['abc\\", "selector list needs"),
        ("$['abc", "unterminated JSONPath string literal"),
    ],
)
def test_unterminated_string_literal_names_the_error(expression: str, message: str) -> None:
    with pytest.raises(JSONPathError, match=message):
        compile_jsonpath(expression)


def test_comparison_operators_are_derived_from_equality_and_less_than() -> None:
    document = {"values": [0], "obj": {"x": "y"}, "arr": [2, 3]}
    assert jsonpath("$.values[?$.absent1 <= $.absent2]", document) == [0]
    assert jsonpath("$.values[?$.obj <= $.obj]", document) == [0]
    assert jsonpath("$.values[?$.arr >= $.arr]", document) == [0]
    assert jsonpath("$.values[?true <= true]", document) == [0]


@pytest.mark.parametrize(
    "expression",
    [
        "store.book",
        "$[",
        "$[9007199254740992]",
        "$.items[?unknown(@)]",
        "$.items[?@.price <]",
        "$.items[?length(@.*)]",
        "$.items[?value(@.name)]",
        "$.items[?match(@.name, 'a.*') == true]",
        "$.items[?!!@.name]",
        "$['\\uD800']",
        "$['\\uDC00']",
        "$['\\u12xz']",
        "$['\\uD800\\u12xz']",
        "$['\\uD800\\u0041']",
        "$['unterminated",
        "$['\\x20']",
        '$["\\\'"]',
    ],
)
def test_invalid_or_ill_typed_jsonpath_is_refused_when_compiled(expression) -> None:
    with pytest.raises(JSONPathError):
        compile_jsonpath(expression)


def test_literal_control_and_surrogate_characters_are_refused() -> None:
    for character in (chr(0x1F), chr(0xD800)):
        with pytest.raises(JSONPathError, match="non-scalar character"):
            compile_jsonpath("$['" + character + "']")


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("$['\\u12", "Unicode escape needs four hexadecimal digits"),
        ("$['\\uD800']", "high surrogate must be followed"),
        ("$['\\uD800\\u12", "low surrogate needs four hexadecimal digits"),
    ],
)
def test_invalid_unicode_escapes_name_the_required_form(expression: str, message: str) -> None:
    with pytest.raises(JSONPathError, match=message):
        compile_jsonpath(expression)


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("$.items[?unknown(@)]", "unknown JSONPath function"),
        ("$.items[?length()]", "needs 1 argument"),
        ("$.items[?length(@.*)]", "singular query"),
        ("$.items[?match(@.*, 'x')]", "value or singular-query arguments"),
        ("$.items[?count(1)]", "needs a query argument"),
    ],
)
def test_ill_typed_functions_name_the_required_form(expression: str, message: str) -> None:
    with pytest.raises(JSONPathError, match=message):
        compile_jsonpath(expression)


def test_value_functions_accept_literal_arguments() -> None:
    assert jsonpath("$[?length(1) == 1]", [1]) == []
    assert jsonpath("$[?match('a', 'a')]", [1]) == [1]


def test_query_evaluation_has_a_configurable_node_visit_ceiling() -> None:
    compiled = compile_jsonpath("$..*", max_visits=3)
    with pytest.raises(JSONPathError, match="visit limit"):
        compiled.find({"a": {"b": 1}, "c": 2})
    with pytest.raises(JSONPathError, match="visit limit"):
        compiled._find_reference({"a": {"b": 1}, "c": 2})


@pytest.mark.parametrize(
    "expression",
    [
        "$",
        "$..author",
        "$.store.book[0,2,-1].author",
        "$.store.book[1:4:2].author",
        "$.store.book[::-1].author",
        "$.store.book[99]",
        "$.store[0]",
        "$.store[0:1]",
        "$.store.book[0:2:0]",
        "$.store.book[*].author",
        "$..*",
        "$.store.book[?@.isbn].author",
        "$.store.book[?@.price < 10 && !(@.category == 'fiction')].author",
        "$.store.book[?match(@.author, '.*Waugh')].author",
    ],
)
def test_native_evaluation_matches_the_independent_reference(expression: str) -> None:
    compiled = compile_jsonpath(expression)
    assert compiled.find(STORE) == compiled._find_reference(STORE)


def test_the_native_jsonpath_entry_refuses_a_malformed_program() -> None:
    import re

    with pytest.raises(JSONPathError, match="program|segment"):
        native_jsonpath_find(
            ("not-a-segment",),
            {},
            10,
            JSONPathError,
            re.fullmatch,
            re.search,
            re.PatternError,
        )
