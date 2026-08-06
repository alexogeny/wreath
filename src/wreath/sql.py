"""SQL that cannot carry an injection, because the values never enter the text.

Every SQL injection has the same shape: a value the caller supplied was turned
into *syntax* instead of being sent as data. The defence has been known for
decades -- bind the value, do not format it -- and applications keep getting it
wrong anyway, because the two spellings look almost identical:

```python
sql = f"SELECT id FROM shipments WHERE reference ILIKE '%{needle}%'"   # a hole
sql = "SELECT id FROM shipments WHERE reference ILIKE $1"              # not
```

The first one is shorter, it reads better, it is what an f-string is *for*, and
nothing about it is flagged by the language, the type checker, or the driver. It
is a defect that has to be caught by a person noticing, every time, forever.

Python 3.14 finally makes the difference visible to the machine. A t-string
(PEP 750) is not a string: it evaluates to a `string.templatelib.Template` that
keeps the literal parts and the interpolated values *apart*, exactly as the
compiler saw them. So a template can be turned into `$1`-style SQL mechanically,
and the interpolated values can only ever leave as parameters:

```python
from wreath.sql import Identifier, Statement

rows = await db.raw(
    t"SELECT id, reference FROM {Identifier(schema, 'shipments')} "
    t"WHERE org_id = {org} AND reference ILIKE {pattern}"
).fetch()
```

`org` and `pattern` become `$1` and `$2`. There is no argument the caller can
pass -- no quote, no comment marker, no `UNION` -- that changes the statement,
because their text is never concatenated into it. Changing that `t` back to an
`f` does not quietly reintroduce the hole either: the result is a `str`, and
`Statement` refuses a `str` with a message naming the fix.

Three interpolated types are spliced into the text rather than bound, because
PostgreSQL resolves them at parse time and a parameter cannot stand in:

* `Identifier` -- a schema, table, or column name, double-quoted and escaped.
* `Fragment` -- literal SQL, the audited escape hatch (see its docstring).
* another `Template` or `Statement` -- a nested clause, whose own parameters are
  renumbered into the outer statement, so clauses compose.

Related: `wreath.hardening` is the rule set that finds the `f`-string spelling
in an application's own source, at boot and in CI, and `wreath.orm.Session.raw`
is the usual place a `Statement` is handed to the database.
"""

from __future__ import annotations

from string.templatelib import Interpolation, Template
from typing import Any

__all__ = ["Fragment", "Identifier", "Statement"]


class Identifier:
    """A schema, table, or column name, quoted for interpolation into SQL.

    A parameter cannot name a relation: PostgreSQL resolves identifiers while it
    parses, long before a bind value exists. So an application that computes a
    table or schema name has to put it in the text, and this is the safe way to
    do it -- the name is double-quoted, any embedded quote is doubled, and a NUL
    is refused outright.

    Several parts qualify a name: `Identifier("northwind", "shipments")` renders
    `"northwind"."shipments"`.

    Quoting makes a hostile name harmless rather than rejected; it becomes a
    relation that does not exist, and the statement fails with an ordinary
    "relation does not exist" instead of running something else. Deciding
    *which* names a caller may reach is a separate question, and one this class
    deliberately does not answer -- look the name up in a directory the
    application owns, as `wreath.orm.TenantContext` does.
    """

    __slots__ = ("parts",)

    def __init__(self, *parts: str) -> None:
        if not parts:
            raise ValueError("Identifier() needs at least one name part")
        for part in parts:
            if not isinstance(part, str):
                raise TypeError(f"identifier parts must be str, got {type(part).__name__}")
            if not part:
                raise ValueError("an identifier part cannot be empty")
            if "\x00" in part:
                raise ValueError("an identifier part cannot contain a NUL byte")
        self.parts: tuple[str, ...] = parts

    @property
    def text(self) -> str:
        """The quoted SQL for this name."""
        return ".".join('"' + part.replace('"', '""') + '"' for part in self.parts)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Identifier) and other.parts == self.parts

    def __hash__(self) -> int:
        return hash((Identifier, self.parts))

    def __repr__(self) -> str:
        return f"Identifier({', '.join(repr(part) for part in self.parts)})"


class Fragment:
    """Literal SQL spliced into a statement without quoting or binding.

    This is the escape hatch, and it is deliberately the only one. Some SQL is
    genuinely syntax rather than data -- `ASC`, `NULLS LAST`, a whole `ORDER BY`
    clause assembled from an allow-list -- and no amount of binding can express
    it.

    A `Fragment` is not a defence. Its whole purpose is to put caller-influenced
    text into the statement, so **the value must come from the application, not
    from the request**: look the caller's choice up in a mapping the application
    owns and pass the mapped value.

    ```python
    DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
    order = Fragment(DIRECTIONS[requested])       # KeyError, not an injection
    ```

    Nothing about a `Fragment` is checked, and that is why it is spelled as a
    type rather than allowed implicitly: it is one greppable word that says
    "this text is SQL on purpose", so a review can find every one of them in a
    tree and read the mapping behind it.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError(f"Fragment() takes str SQL, got {type(text).__name__}")
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Fragment) and other.text == self.text

    def __hash__(self) -> int:
        return hash((Fragment, self.text))

    def __repr__(self) -> str:
        return f"Fragment({self.text!r})"


class Statement:
    """SQL text with `$1`-style placeholders, compiled from a t-string.

    `text` is what goes to the server and `args` is what is bound to it. The
    split is decided by the compiler, from the source, so no runtime value can
    move itself from one side to the other.
    """

    __slots__ = ("args", "text")

    def __init__(self, template: Template) -> None:
        if not isinstance(template, Template):
            raise TypeError(
                "Statement() takes a t-string (PEP 750), not "
                f"{type(template).__name__}. Write t\"SELECT ... {{value}}\" rather "
                'than f"SELECT ... {value}": an f-string has already pasted the '
                "value into the SQL by the time it gets here, which is the "
                "injection this type exists to make unwritable."
            )
        parts: list[str] = []
        args: list[Any] = []
        _render(template, parts, args)
        self.text: str = "".join(parts)
        self.args: tuple[Any, ...] = tuple(args)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Statement)
            and other.text == self.text
            and other.args == self.args
        )

    def __hash__(self) -> int:
        return hash((Statement, self.text, self.args))

    def __repr__(self) -> str:
        count = len(self.args)
        plural = "" if count == 1 else "s"
        return f"Statement({self.text!r}, {count} parameter{plural})"


def _render(template: Template, parts: list[str], args: list[Any]) -> None:
    """Append `template`'s text to `parts` and its bound values to `args`.

    Recursive rather than iterative because nesting is how clauses compose, and
    the recursion is bounded by how deeply the *source* nests templates -- a
    depth the compiler fixed, not one a request can grow.
    """
    for item in template:
        if isinstance(item, str):
            parts.append(item)
            continue
        value = item.value
        if isinstance(value, (Identifier, Fragment)):
            _check_plain(item, type(value).__name__)
            parts.append(value.text)
        elif isinstance(value, Template):
            _check_plain(item, "nested template")
            _render(value, parts, args)
        elif isinstance(value, Statement):
            _check_plain(item, "nested statement")
            parts.append(_renumber(value.text, len(args)))
            args.extend(value.args)
        else:
            _check_plain(item, "bound value")
            args.append(value)
            parts.append(f"${len(args)}")


def _check_plain(item: Interpolation, what: str) -> None:
    """Refuse `!r` and `:spec` on an interpolation.

    Both ask for text formatting, and neither has anywhere to happen: a bound
    value is sent to the server as data, and a spliced one is already SQL.
    Honouring the syntax would mean formatting the value into the statement --
    the exact move this module exists to prevent -- and ignoring it would mean
    silently dropping something the author wrote on purpose.
    """
    if item.conversion:
        raise ValueError(
            f"{{{item.expression}!{item.conversion}}} asks for a conversion on a "
            f"{what}, which is never formatted into the SQL; drop the "
            f"!{item.conversion}"
        )
    if item.format_spec:
        raise ValueError(
            f"{{{item.expression}:{item.format_spec}}} carries a format "
            f"specification on a {what}, which is never formatted into the SQL; "
            "format the value before interpolating it, or drop the spec"
        )


def _renumber(text: str, offset: int) -> str:
    """Shift every `$n` in `text` up by `offset`.

    A nested `Statement` was compiled on its own, so its parameters start at
    `$1`; spliced into an outer statement they have to continue the outer
    numbering. A nested `Template` needs none of this because it is rendered
    inline and never had its own numbering.
    """
    if offset == 0:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "$":
            out.append(char)
            index += 1
            continue
        digits = index + 1
        while digits < length and text[digits].isdigit():
            digits += 1
        if digits == index + 1:
            out.append(char)
            index += 1
            continue
        out.append(f"${int(text[index + 1 : digits]) + offset}")
        index = digits
    return "".join(out)
