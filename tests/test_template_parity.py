from __future__ import annotations

import pytest

from wreath._native import _core
from wreath._template_tape import Markup, TemplateRenderError, compile_tape
from wreath.templates import (
    Template,
    TemplateDirectory,
    TemplateSyntaxError,
    escape,
)

# Configure the engine explicitly, so escaping and error construction match
# whatever this module holds independent of import order in the facade.
_core.template_configure(Markup, TemplateRenderError)

_LIMIT = 16 * 1024 * 1024


def render(tape: tuple, context: dict, max_output: int = _LIMIT) -> bytes:
    """Render `tape` both ways, assert the two agree, and return the bytes."""
    walked = _core.template_render(tape, context, max_output)
    compiled = _core.template_render_compiled(_core.template_compile(tape), context, max_output)
    assert compiled == walked, "the compiled program disagreed with the walked tape"
    return walked


def render_error(tape: tuple, context: dict, max_output: int = _LIMIT) -> BaseException:
    """The error both paths raise, asserted identical, and returned."""
    walked = _capture(lambda: _core.template_render(tape, context, max_output))
    program = _core.template_compile(tape)
    compiled = _capture(lambda: _core.template_render_compiled(program, context, max_output))
    assert type(compiled) is type(walked)
    assert str(compiled) == str(walked)
    return walked


class Obj:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


PARITY_CASES = [
    ("<h1>{{ title }}</h1>", {"title": "A & B <script>\"'"}),
    ("plain text & <b> no tags", {}),
    ("{{ n }} items", {"n": 42}),
    ("{{ safe }}", {"safe": Markup("<b>bold</b>")}),
    (
        "{% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.msg }}</td></tr>{% endfor %}",
        {"rows": [{"id": 1, "msg": "a<b"}, {"id": 2, "msg": "c&d'e"}]},
    ),
    ("{% if show %}yes {{ name }}{% else %}no{% endif %}", {"show": True, "name": "x'y"}),
    ("{% if show %}yes{% else %}no {{ name }}{% endif %}", {"show": False, "name": "z&z"}),
    ("{% for x in xs %}{{ x }}{% endfor %}done", {"xs": []}),
    ("{% for x in xs %}{{ x }}{% endfor %}", {"xs": (1, 2, 3)}),
    ("{% for x in xs %}{{ x }}{% endfor %}", {"xs": range(1, 4)}),
    (
        "{% for o in items %}[{% for c in o.cs %}{{ c }},{% endfor %}]{% endfor %}",
        {"items": [{"cs": [1, 2]}, {"cs": [3]}]},
    ),
    (
        "{% for x in xs %}{{ x }}{% endfor %}|{{ x }}",
        {"xs": [1, 2], "x": "context"},
    ),
    (
        "{% for x in xs %}{{ x.name }}:{% for x in x.children %}{{ x }}"
        "{% endfor %}:{{ x.name }};{% endfor %}",
        {"xs": [{"name": "outer", "children": [1, 2]}]},
    ),
    ("{{ obj.attr }}", {"obj": Obj(attr="<danger>")}),
    ("café — {{ v }} 日本語", {"v": "héllo—é"}),
    ("{% if a %}{% if b %}AB{% endif %}{% endif %}", {"a": 1, "b": 1}),
    ("{% if a %}{% if b %}AB{% endif %}{% endif %}", {"a": 1, "b": 0}),
]


@pytest.mark.parametrize("source, context", PARITY_CASES)
def test_both_execution_paths_render_one_answer(source: str, context: dict) -> None:
    assert isinstance(render(compile_tape(source), context), bytes)


class Weird(int):
    """An int whose `str` is not its digits, so `str()` is not substitutable."""

    def __str__(self) -> str:
        return "weird"


#: One per kind the emitter may treat specially. `str(value)` is the contract,
#: so anything that answers `__str__` differently from its digits -- `bool`,
#: an `int` subclass -- must not take a digits fast path, and an integer wider
#: than a C long must not be truncated to fit one.
NUMERIC_CASES = [
    {"v": 0},
    {"v": 7},
    {"v": -7},
    {"v": 2**62},
    {"v": 2**63},  # one past a signed C long
    {"v": -(2**63) - 1},
    {"v": 2**200 + 12345},  # far past any machine word
    {"v": True},  # `str(True)` is "True", not "1"
    {"v": False},
    {"v": Weird(5)},  # `str` is overridden; digits would be wrong
    {"v": 1.5},
    {"v": float("inf")},
]


@pytest.mark.parametrize("context", NUMERIC_CASES, ids=lambda case: repr(case["v"]))
def test_non_string_values_render_as_str(context: dict) -> None:
    # Against `str()`, which is the contract, rather than against the other
    # execution path -- the two agreeing is a separate and weaker claim.
    assert render(compile_tape("{{ v }}"), context) == str(context["v"]).encode()


def test_a_number_inside_a_loop_renders_once_per_row() -> None:
    tape = compile_tape("{% for r in rows %}<i>{{ r.id }}</i>{% endfor %}")
    rows = [{"id": index} for index in range(13)]
    expected = b"".join(f"<i>{index}</i>".encode() for index in range(13))
    assert render(tape, {"rows": rows}, 1 << 20) == expected


@pytest.mark.parametrize(
    "source, context",
    [
        ("{{ missing }}", {}),
        ("{{ a.b }}", {"a": {"z": 1}}),
        ("{% for x in v %}{% endfor %}", {"v": 5}),
    ],
)
def test_a_bad_lookup_is_a_render_error_on_both_paths(source: str, context: dict) -> None:
    assert isinstance(render_error(compile_tape(source), context), TemplateRenderError)


def test_output_past_the_size_bound_is_refused_on_both_paths() -> None:
    tape = compile_tape("{{ x }}")
    assert isinstance(render_error(tape, {"x": "a" * 100}, 10), TemplateRenderError)


def test_compiled_render_appends_a_tail_in_the_output_allocation() -> None:
    template = Template.from_string("<p>{{ value }}</p>")

    assert template._render_bytes_tail({"value": "a&b"}, b"<footer>x</footer>") == (
        b"<p>a&amp;b</p><footer>x</footer>"
    )
    with pytest.raises(TemplateRenderError, match="output too large"):
        template._render_bytes_tail({"value": "a&b"}, b"tail", max_output=8)


def test_escaping_covers_all_five() -> None:
    assert escape("<a href=\"x\">'&'</a>") == (
        "&lt;a href=&#34;x&#34;&gt;&#39;&amp;&#39;&lt;/a&gt;"
    )


def test_markup_is_not_escaped() -> None:
    template = Template.from_string("{{ value }}")
    assert template.render(value=Markup("<i>x</i>")) == "<i>x</i>"
    assert template.render(value="<i>x</i>") == "&lt;i&gt;x&lt;/i&gt;"


def test_plain_strings_are_untrusted() -> None:
    template = Template.from_string("<p>{{ comment }}</p>")
    rendered = template.render(comment="<script>alert(1)</script>")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{% if x %}no end", "unclosed 'if' block"),
        ("{% for x in xs %}no end", "unclosed 'for' block"),
        ("{% endif %}", "'endif' without 'if'"),
        ("{% endfor %}", "'endfor' without 'for'"),
        ("{% else %}", "'else' outside of 'if'"),
        ("{{ }}", "empty expression"),
        ("{{ a b }}", "invalid lookup path 'a b'"),
        ("{% bogus %}", "unknown tag 'bogus'"),
        ("{% include x %}", "include target must be a quoted string"),
        # Below: refusals no source reached. Each is a *different* branch that
        # the closest existing case above never entered -- `{% else %}` alone
        # leaves `blocks` empty, so it refuses one tag earlier than a second
        # `else` inside an `if` does, and the two say different things.
        ("{% %}", "empty tag"),
        ("{% if a %}{% else %}{% else %}{% endif %}", "duplicate 'else'"),
        ("{% for x in y %}{% else %}{% endfor %}", "'else' outside of 'if'"),
        ("{% for x in y %}{% endif %}", "'endif' without 'if'"),
        ("{% if a %}{% endfor %}", "'endfor' without 'for'"),
        ("{% for 1x in items %}{% endfor %}", "invalid loop variable '1x'"),
        ('{% include "x.html" %}', "include requires a TemplateDirectory"),
        ("{{ name", "unterminated tag"),
        ("{% if a", "unterminated tag"),
    ],
)
def test_syntax_errors(source: str, message: str) -> None:
    with pytest.raises(TemplateSyntaxError) as caught:
        Template.from_string(source)
    assert message in str(caught.value)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("no braces here", "no braces here"),
        ("a { b", "a { b"),
        ("trailing brace {", "trailing brace {"),
        ("{", "{"),
        ("50% off {but not a tag}", "50% off {but not a tag}"),
    ],
)
def test_a_brace_that_opens_no_tag_is_literal_text(source: str, expected: str) -> None:
    assert render(compile_tape(source), {}) == expected.encode()


def test_text_on_both_sides_of_a_tag_survives() -> None:
    assert render(compile_tape("before {{ a }} after"), {"a": "X"}) == b"before X after"


def test_a_lone_brace_does_not_swallow_a_later_tag() -> None:
    tape = compile_tape("style { color: red } and {{ name }}")
    assert render(tape, {"name": "Andie"}) == b"style { color: red } and Andie"


def test_a_false_condition_with_an_else_renders_the_else_body() -> None:
    tape = compile_tape("{% if flag %}yes{% else %}no{% endif %}")
    assert render(tape, {"flag": False}) == b"no"
    assert render(tape, {"flag": True}) == b"yes"


def test_directory_include_resolved_at_compile(tmp_path) -> None:
    (tmp_path / "row.html").write_text("<td>{{ r.msg }}</td>")
    (tmp_path / "table.html").write_text(
        '<table>{% for r in rows %}<tr>{% include "row.html" %}</tr>{% endfor %}</table>'
    )
    directory = TemplateDirectory(tmp_path)
    template = directory.compile("table.html")
    out = template.render(rows=[{"msg": "a<b"}, {"msg": "c"}])
    assert out == "<table><tr><td>a&lt;b</td></tr><tr><td>c</td></tr></table>"


def test_directory_rejects_traversal(tmp_path) -> None:
    with pytest.raises(TemplateSyntaxError):
        TemplateDirectory(tmp_path).compile("../etc/passwd")


def test_include_cycle_detected(tmp_path) -> None:
    (tmp_path / "a.html").write_text('{% include "b.html" %}')
    (tmp_path / "b.html").write_text('{% include "a.html" %}')
    with pytest.raises(TemplateSyntaxError):
        TemplateDirectory(tmp_path).compile("a.html")


def _capture(fn):
    try:
        fn()
    except Exception as error:  # noqa: BLE001 - returned for comparison
        return error
    return None


def test_directory_rejects_symlink_escape(tmp_path):
    import os as _os

    from wreath.templates import TemplateDirectory, TemplateSyntaxError

    root = tmp_path / "templates"
    root.mkdir()
    (root / "ok.html").write_text("hello", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    _os.symlink(secret, root / "link.html")

    directory = TemplateDirectory(root)
    assert directory.compile("ok.html").render() == "hello"
    try:
        directory.compile("link.html")
    except TemplateSyntaxError as exc:
        assert "SECRET" not in str(exc)
    else:
        raise AssertionError("symlinked template was compiled")


def test_include_rejects_symlink_escape(tmp_path):
    import os as _os

    from wreath.templates import TemplateDirectory, TemplateSyntaxError

    root = tmp_path / "templates"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    _os.symlink(secret, root / "leak.html")
    (root / "parent.html").write_text("{% include 'leak.html' %}", encoding="utf-8")

    directory = TemplateDirectory(root)
    try:
        directory.compile("parent.html")
    except TemplateSyntaxError as exc:
        assert "SECRET" not in str(exc)
    else:
        raise AssertionError("template included a symlinked file")


@pytest.mark.parametrize(
    "source",
    [
        "{{ u.__class__ }}",
        "{{ u.__init__.__globals__.API_KEY }}",
        "{{ __class__ }}",  # first segment: getattr on the context dict
        "{{ u._private }}",
        "{% if u.__dict__ %}x{% endif %}",
        "{% for x in u._items %}{{ x }}{% endfor %}",
    ],
)
def test_a_template_cannot_walk_into_private_attributes(source: str) -> None:
    with pytest.raises(TemplateSyntaxError) as caught:
        Template.from_string(source)
    assert "private name" in str(caught.value)


def test_the_private_name_rule_is_enforced_before_a_tape_exists() -> None:
    with pytest.raises(TemplateSyntaxError):
        compile_tape("{{ u.__class__ }}")


def test_ordinary_dotted_lookups_still_resolve() -> None:
    class User:
        def __init__(self) -> None:
            self.name = "alex"
            self.tags = {"role": "owner"}

    assert Template.from_string("{{ u.name }}/{{ u.tags.role }}").render(u=User()) == ("alex/owner")


def test_custom_subscript_still_precedes_attribute_lookup() -> None:
    class User:
        name = "attribute"

        def __getitem__(self, key: str) -> str:
            return f"item:{key}"

    assert Template.from_string("{{ u.name }}").render(u=User()) == "item:name"
