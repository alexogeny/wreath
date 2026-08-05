"""Template engine behaviour and native/pure byte-parity.

The pure VM in wreath._pure.templates is the reference; the native engine must
produce byte-identical UTF-8 and raise the same errors. Parity cases run
against both engines directly so the comparison does not depend on which one
the facade happens to select.
"""

from __future__ import annotations

import pytest

from wreath._native import _core
from wreath._pure.templates import Markup as PureMarkup
from wreath._pure.templates import (
    TemplateRenderError,
    compile_tape,
    render_tape,
)
from wreath.templates import (
    Markup,
    Template,
    TemplateDirectory,
    TemplateSyntaxError,
    escape,
)

_HAS_NATIVE = _core is not None and hasattr(_core, "template_render")

if _HAS_NATIVE:
    # Configure the native engine with the pure types so escaping and error
    # construction match exactly, independent of import order in the facade.
    _core.template_configure(PureMarkup, TemplateRenderError)


class Obj:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


PARITY_CASES = [
    ("<h1>{{ title }}</h1>", {"title": "A & B <script>\"'"}),
    ("plain text & <b> no tags", {}),
    ("{{ n }} items", {"n": 42}),
    ("{{ safe }}", {"safe": PureMarkup("<b>bold</b>")}),
    (
        "{% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.msg }}</td></tr>{% endfor %}",
        {"rows": [{"id": 1, "msg": "a<b"}, {"id": 2, "msg": "c&d'e"}]},
    ),
    ("{% if show %}yes {{ name }}{% else %}no{% endif %}", {"show": True, "name": "x'y"}),
    ("{% if show %}yes{% else %}no {{ name }}{% endif %}", {"show": False, "name": "z&z"}),
    ("{% for x in xs %}{{ x }}{% endfor %}done", {"xs": []}),
    (
        "{% for o in items %}[{% for c in o.cs %}{{ c }},{% endfor %}]{% endfor %}",
        {"items": [{"cs": [1, 2]}, {"cs": [3]}]},
    ),
    ("{{ obj.attr }}", {"obj": Obj(attr="<danger>")}),
    ("café — {{ v }} 日本語", {"v": "héllo—é"}),
    ("{% if a %}{% if b %}AB{% endif %}{% endif %}", {"a": 1, "b": 1}),
    ("{% if a %}{% if b %}AB{% endif %}{% endif %}", {"a": 1, "b": 0}),
]


@pytest.mark.parametrize("source, context", PARITY_CASES)
def test_pure_native_byte_parity(source: str, context: dict) -> None:
    tape = compile_tape(source)
    pure = render_tape(tape, context)
    assert isinstance(pure, bytes)
    if _HAS_NATIVE:
        native = _core.template_render(tape, context, 16 * 1024 * 1024)
        assert native == pure


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
    {"v": 2**63},                 # one past a signed C long
    {"v": -(2**63) - 1},
    {"v": 2**200 + 12345},        # far past any machine word
    {"v": True},                  # `str(True)` is "True", not "1"
    {"v": False},
    {"v": Weird(5)},              # `str` is overridden; digits would be wrong
    {"v": 1.5},
    {"v": float("inf")},
]


@pytest.mark.parametrize("context", NUMERIC_CASES, ids=lambda case: repr(case["v"]))
def test_non_string_values_render_as_str_in_both_engines(context: dict) -> None:
    tape = compile_tape("{{ v }}")
    pure = render_tape(tape, context)
    assert pure == str(context["v"]).encode()
    if _HAS_NATIVE:
        assert _core.template_render(tape, context, 16 * 1024 * 1024) == pure


def test_a_number_inside_a_loop_renders_once_per_row() -> None:
    """The loop body is where a decoded tape would be reused across rows.

    Rendering the same instruction thirteen times must produce thirteen
    distinct values, not the first one repeated -- which is the way a
    pre-decoded instruction stream fails when an operand is cached too eagerly.
    """
    tape = compile_tape("{% for r in rows %}<i>{{ r.id }}</i>{% endfor %}")
    rows = [{"id": index} for index in range(13)]
    expected = b"".join(f"<i>{index}</i>".encode() for index in range(13))
    assert render_tape(tape, {"rows": rows}) == expected
    if _HAS_NATIVE:
        assert _core.template_render(tape, {"rows": rows}, 1 << 20) == expected


@pytest.mark.parametrize(
    "source, context",
    [
        ("{{ missing }}", {}),
        ("{{ a.b }}", {"a": {"z": 1}}),
        ("{% for x in v %}{% endfor %}", {"v": 5}),
    ],
)
def test_error_parity(source: str, context: dict) -> None:
    tape = compile_tape(source)
    pure_err = _capture(lambda: render_tape(tape, context))
    assert isinstance(pure_err, TemplateRenderError)
    if _HAS_NATIVE:
        native_err = _capture(
            lambda: _core.template_render(tape, context, 16 * 1024 * 1024)
        )
        assert type(native_err) is type(pure_err)
        assert str(native_err) == str(pure_err)


def test_output_size_overflow_both_engines() -> None:
    tape = compile_tape("{{ x }}")
    context = {"x": "a" * 100}
    assert isinstance(_capture(lambda: render_tape(tape, context, 10)), TemplateRenderError)
    if _HAS_NATIVE:
        assert isinstance(
            _capture(lambda: _core.template_render(tape, context, 10)),
            TemplateRenderError,
        )


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
    "source",
    [
        "{% if x %}no end",
        "{% for x in xs %}no end",
        "{% endif %}",
        "{% endfor %}",
        "{% else %}",
        "{{ }}",
        "{{ a b }}",
        "{% bogus %}",
        "{% include x %}",
    ],
)
def test_syntax_errors(source: str) -> None:
    with pytest.raises(TemplateSyntaxError):
        Template.from_string(source)


def test_directory_include_resolved_at_compile(tmp_path) -> None:
    (tmp_path / "row.html").write_text("<td>{{ r.msg }}</td>")
    (tmp_path / "table.html").write_text(
        "<table>{% for r in rows %}<tr>{% include \"row.html\" %}</tr>{% endfor %}</table>"
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


# --- adversarial: template loading must not follow symlinks out of root (#8) --

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
        "{{ __class__ }}",                      # first segment: getattr on the context dict
        "{{ u._private }}",
        "{% if u.__dict__ %}x{% endif %}",
        "{% for x in u._items %}{{ x }}{% endfor %}",
    ],
)
def test_a_template_cannot_walk_into_private_attributes(source: str) -> None:
    """A lookup falls back from subscript to `getattr`, so a dotted path could
    walk an object's internals -- `u.__init__.__globals__.API_KEY` read a module
    global straight into the output. Refused at compile time, which is also why
    the native engine needs no rule of its own: it only executes the tape."""
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

    assert Template.from_string("{{ u.name }}/{{ u.tags.role }}").render(u=User()) == (
        "alex/owner")
