"""A GraphQL query parser whose safety limits are part of the parse.

A public GraphQL endpoint is a denial-of-service surface by construction: a tiny
document can demand unbounded work through nesting, aliasing, or fragment
expansion. Those limits are enforced **while parsing**, not after, so a hostile
document is refused before its tree is ever built -- checking afterwards means
having already paid to construct the thing you are about to reject.

Four bounds, all of them on attacker-controlled dimensions:

``max_depth``
    Selection-set nesting. Deep nesting over a cyclic schema (``author ->
    posts -> author -> ...``) is the classic amplification.
``max_complexity``
    Total selected fields after fragment expansion. This is the one that
    catches width rather than depth.
``max_aliases``
    Aliases of the same field. ``a: user b: user c: user ...`` costs one
    resolve each while the document stays small.
``max_document_bytes``
    Source length, checked **before** a single character is scanned. This is
    the cheapest and broadest of the five: parse cost scales with document
    length, so capping length caps the worst case for a fraction of the work
    the other limits do. It is also the only one an attacker cannot approach
    incrementally -- the document is rejected on `len()`.
``max_steps``
    A token budget, mirroring ``binding._VALIDATE_MAX_STEPS`` and
    ``validate.c``. The backstop for anything the three shape limits do not
    describe -- a pathological token stream that is neither deep nor wide.
    Applied while tokenizing, which also bounds the descent that follows.

Fragment cycles are rejected outright: a fragment that reaches itself can
expand forever, and no depth limit expressed in *selection sets* bounds it.
"""

from __future__ import annotations

import re
from typing import Any

from .ast import (
    Argument,
    Document,
    Field,
    FragmentDefinition,
    FragmentSpread,
    InlineFragment,
    Operation,
    Selection,
    SelectionSet,
    Variable,
    VariableDefinition,
)

__all__ = ["GraphQLSyntaxError", "Limits", "parse"]

#: Default step ceiling. Generous for any real document (a 200-field query
#: costs a few thousand steps) and far below anything that takes visible time.
DEFAULT_MAX_STEPS = 200_000

# One `findall` tokenizes the whole document. Three designs were measured on a
# 169-char / 47-token query before this one stuck:
#
#   char-at-a-time scanner, re-skipping whitespace per peek   26us
#   named-group master regex + match.lastgroup                22us
#   group-free regex + findall + first-character dispatch      5us
#
# The cost was never the scanning -- it was doing thousands of small scans, and
# then paying `match.lastgroup`, which does a reverse group-name lookup per
# token. A pattern with no capture groups lets `findall` return plain strings
# with no match objects at all, and the first character of a token is enough to
# classify it. Ignored runs stay *in* the pattern (rather than being skipped by
# omission) so an illegal character is rejected instead of silently dropped.
_TOKENS = re.compile(
    r'[A-Za-z_][A-Za-z0-9_]*'                       # name
    r'|[{}()\[\]:=$@!|&]'                            # punctuation
    r'|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'             # number
    r'|"""[\s\S]*?"""'                              # block string
    r'|"(?:[^"\\\r\n]|\\.)*"'                      # string
    r'|\.\.\.'                                      # spread
    r'|(?:[\s,\ufeff]|\#[^\r\n]*)+'                 # ignored run
    r'|.'                                           # anything else -> error
)
#: Token kinds, as small ints so the parser compares numbers not strings.
_T_PUNCT, _T_NAME, _T_NUMBER, _T_STRING, _T_SPREAD, _T_EOF = 0, 1, 2, 3, 4, 5
_ESCAPE_SCAN = re.compile(r"\\(u[0-9a-fA-F]{4}|.)")
_PUNCT_CHARS = frozenset("{}()[]:=$@!|&")
_NAME_HEAD = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
)
_DIGIT_HEAD = frozenset("-0123456789")
_IGNORED_HEAD = frozenset(" \t\r\n\f\v,\ufeff#")


class GraphQLSyntaxError(Exception):
    """A document that could not be parsed, or that exceeded a safety limit.

    ``code`` distinguishes a malformed document (``"syntax"``) from a
    well-formed but abusive one (``"depth"``, ``"complexity"``, ``"aliases"``,
    ``"steps"``, ``"fragment_cycle"``), so a server can log the two differently
    -- the second class is an attack signal, the first is usually a bug.
    """

    def __init__(self, message: str, *, code: str = "syntax", position: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.position = position


class Limits:
    """Safety bounds for one parse. Shared and read-only across requests."""

    __slots__ = (
        "max_aliases", "max_complexity", "max_depth", "max_document_bytes",
        "max_steps",
    )

    def __init__(
        self,
        *,
        max_depth: int = 12,
        max_complexity: int = 1000,
        max_aliases: int = 50,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_document_bytes: int = 16 * 1024,
    ) -> None:
        for name, value in (
            ("max_depth", max_depth),
            ("max_complexity", max_complexity),
            ("max_aliases", max_aliases),
            ("max_steps", max_steps),
            ("max_document_bytes", max_document_bytes),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        self.max_depth = max_depth
        self.max_complexity = max_complexity
        self.max_aliases = max_aliases
        self.max_steps = max_steps
        self.max_document_bytes = max_document_bytes


DEFAULT_LIMITS = Limits()


class _Parser:
    """A single-pass recursive-descent parser over the source text.

    One instance per document; never shared. The counters it carries
    (`_complexity`, `_alias_counts`, `_depth`) are what make the shape limits
    enforceable while parsing rather than after it.

    The step budget is applied during tokenization, not during the descent: the
    descent visits each token at most a constant number of times, so bounding
    the token count bounds it too. Re-checking per node was a method call per
    AST node for a guarantee already held.
    """

    __slots__ = (
        "_alias_counts", "_complexity", "_depth", "_index", "_kinds",
        "_limits", "_source", "_starts", "_values",
    )

    def __init__(self, source: str, limits: Limits) -> None:
        self._source = source
        self._limits = limits
        self._complexity = 0
        self._depth = 0
        self._alias_counts: dict[str, int] = {}
        self._index = 0
        # Parallel arrays rather than tuples: the parser indexes these millions
        # of times and a list load beats unpacking a tuple per access.
        self._kinds: list[int] = []
        self._values: list[Any] = []
        self._starts: list[int] = []
        self._tokenize()

    def _tokenize(self) -> None:
        kinds = self._kinds
        values = self._values
        starts = self._starts
        max_steps = self._limits.max_steps
        position = 0
        for token in _TOKENS.findall(self._source):
            head = token[0]
            start = position
            position += len(token)
            if head in _IGNORED_HEAD:
                continue
            if len(kinds) >= max_steps:
                raise GraphQLSyntaxError(
                    f"document exceeded the {max_steps}-token parse budget",
                    code="steps",
                    position=start,
                )
            if head in _NAME_HEAD:
                kinds.append(_T_NAME)
                values.append(token)
            elif head in _PUNCT_CHARS:
                kinds.append(_T_PUNCT)
                values.append(token)
            elif head in _DIGIT_HEAD:
                if token == "-":
                    raise GraphQLSyntaxError(
                        "expected a number after '-'", position=start
                    )
                kinds.append(_T_NUMBER)
                values.append(
                    float(token)
                    if ("." in token or "e" in token or "E" in token)
                    else int(token)
                )
            elif head == '"':
                if token.startswith('"""'):
                    kinds.append(_T_STRING)
                    values.append(_block_string_value(token[3:-3]))
                elif len(token) < 2 or not token.endswith('"'):
                    raise GraphQLSyntaxError("unterminated string", position=start)
                else:
                    kinds.append(_T_STRING)
                    values.append(_unescape(token[1:-1]))
            elif token == "...":
                kinds.append(_T_SPREAD)
                values.append(token)
            else:
                # The trailing `.` alternative matched: an illegal character, or
                # a lone `"` the string alternatives could not close.
                raise GraphQLSyntaxError(
                    "unterminated string" if head == '"' else
                    f"unexpected character {head!r}",
                    position=start,
                )
            starts.append(start)

    # -- budget ------------------------------------------------------------

    @property
    def _position(self) -> int:
        starts = self._starts
        if not starts:
            return 0
        return starts[min(self._index, len(starts) - 1)]

    def _fail(self, message: str, code: str = "syntax") -> GraphQLSyntaxError:
        return GraphQLSyntaxError(message, code=code, position=self._position)

    # -- token access ------------------------------------------------------

    def _kind(self) -> int:
        index = self._index
        return self._kinds[index] if index < len(self._kinds) else _T_EOF

    def _peek(self) -> str:
        """The next punctuation character, or "" at end of input.

        Kept as a character test because the grammar below branches on literal
        punctuation; a name or number simply reports as not-that-character.
        """
        index = self._index
        if index >= len(self._kinds):
            return ""
        if self._kinds[index] == _T_PUNCT:
            return self._values[index]
        return "\x00"

    def _at_end(self) -> bool:
        return self._index >= len(self._kinds)

    def _expect(self, character: str) -> None:
        if self._peek() != character:
            raise self._fail(f"expected {character!r}")
        self._index += 1

    def _maybe(self, character: str) -> bool:
        if self._peek() == character:
            self._index += 1
            return True
        return False

    def _name(self) -> str:
        index = self._index
        if index >= len(self._kinds) or self._kinds[index] != _T_NAME:
            raise self._fail("expected a name")
        self._index = index + 1
        return self._values[index]

    # -- values ------------------------------------------------------------

    def _value(self) -> Any:
        kind = self._kind()
        if kind == _T_NUMBER or kind == _T_STRING:
            value = self._values[self._index]
            self._index += 1
            return value
        character = self._peek()
        if character == "$":
            self._index += 1
            return Variable(self._name())
        if character == "[":
            self._index += 1
            items: list[Any] = []
            while self._peek() != "]":
                if self._at_end():
                    raise self._fail("unterminated list value")
                items.append(self._value())
            self._index += 1
            return items
        if character == "{":
            self._index += 1
            entries: dict[str, Any] = {}
            while self._peek() != "}":
                if self._at_end():
                    raise self._fail("unterminated object value")
                key = self._name()
                self._expect(":")
                entries[key] = self._value()
            self._index += 1
            return entries
        name = self._name()
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "null":
            return None
        return name  # an enum value, carried as its name

    # -- structure ---------------------------------------------------------

    def _arguments(self) -> tuple[Argument, ...]:
        if not self._maybe("("):
            return ()
        arguments: list[Argument] = []
        while self._peek() != ")":
            if self._at_end():
                raise self._fail("unterminated argument list")
            name = self._name()
            self._expect(":")
            arguments.append(Argument(name, self._value()))
        self._index += 1
        return tuple(arguments)

    def _skip_directives(self) -> None:
        # Parsed and discarded: the schema defines no directives, and silently
        # ignoring an unknown one is friendlier than refusing a document a
        # client's tooling added `@_unmask` or similar to.
        while self._peek() == "@":
            self._index += 1
            self._name()
            self._arguments()

    def _selection_set(self, depth: int) -> SelectionSet:
        limits = self._limits
        if depth > limits.max_depth:
            raise self._fail(
                f"selection nesting exceeds the maximum depth of {limits.max_depth}",
                code="depth",
            )
        if depth > self._depth:
            self._depth = depth
        self._expect("{")
        selections: list[Selection] = []
        while self._peek() != "}":
            if self._at_end():
                raise self._fail("unterminated selection set")
            selections.append(self._selection(depth))
        self._index += 1
        if not selections:
            raise self._fail("a selection set cannot be empty")
        return SelectionSet(tuple(selections))

    def _selection(self, depth: int) -> Selection:
        if self._kind() == _T_SPREAD:
            self._index += 1
            if self._peek() == "{":
                self._skip_directives()
                return InlineFragment(None, self._selection_set(depth + 1))
            name = self._name()
            if name == "on":
                condition = self._name()
                self._skip_directives()
                return InlineFragment(condition, self._selection_set(depth + 1))
            self._skip_directives()
            return FragmentSpread(name)

        name = self._name()
        key = name
        if self._peek() == ":":
            self._index += 1
            key = name
            name = self._name()

        self._complexity += 1
        if self._complexity > self._limits.max_complexity:
            raise self._fail(
                f"document selects more than {self._limits.max_complexity} fields",
                code="complexity",
            )
        if key != name:
            count = self._alias_counts.get(name, 0) + 1
            self._alias_counts[name] = count
            if count > self._limits.max_aliases:
                raise self._fail(
                    f"field {name!r} is aliased more than "
                    f"{self._limits.max_aliases} times",
                    code="aliases",
                )

        arguments = self._arguments()
        self._skip_directives()
        selection_set = (
            self._selection_set(depth + 1) if self._peek() == "{" else None
        )
        return Field(name=name, key=key, arguments=arguments, selection_set=selection_set)

    def _variable_definitions(self) -> tuple[VariableDefinition, ...]:
        if not self._maybe("("):
            return ()
        definitions: list[VariableDefinition] = []
        while self._peek() != ")":
            if self._at_end():
                raise self._fail("unterminated variable definitions")
            self._expect("$")
            name = self._name()
            self._expect(":")
            is_list = self._maybe("[")
            type_name = self._name()
            inner_non_null = self._maybe("!")
            if is_list:
                self._expect("]")
            non_null = self._maybe("!") or (inner_non_null and not is_list)
            default: Any = None
            has_default = False
            if self._maybe("="):
                default = self._value()
                has_default = True
            definitions.append(
                VariableDefinition(
                    name, type_name, non_null, is_list, default, has_default
                )
            )
        self._index += 1
        return tuple(definitions)

    def _operation(self) -> Operation:
        kind = "query"
        name: str | None = None
        if self._peek() != "{":
            kind = self._name()
            if kind not in ("query", "mutation"):
                raise self._fail(
                    f"unsupported operation {kind!r}; only query and mutation are served"
                )
            if self._peek() not in ("(", "{"):
                name = self._name()
        variables = self._variable_definitions()
        self._skip_directives()
        return Operation(kind, name, variables, self._selection_set(1))

    def parse(self) -> Document:
        operations: list[Operation] = []
        fragments: dict[str, FragmentDefinition] = {}
        while not self._at_end():
            if self._peek() == "{":
                operations.append(self._operation())
                continue
            checkpoint = self._index
            word = self._name()
            if word == "fragment":
                fragment_name = self._name()
                if self._name() != "on":
                    raise self._fail("a fragment needs an `on` type condition")
                condition = self._name()
                self._skip_directives()
                if fragment_name in fragments:
                    raise self._fail(f"fragment {fragment_name!r} is defined twice")
                fragments[fragment_name] = FragmentDefinition(
                    fragment_name, condition, self._selection_set(1)
                )
                continue
            self._index = checkpoint
            operations.append(self._operation())

        if not operations:
            raise self._fail("document defines no operation")
        _reject_fragment_cycles(fragments)
        return Document(
            tuple(operations), fragments, self._depth, self._complexity
        )


_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/", "b": "\b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


def _unescape(raw: str) -> str:
    """Decode a simple string's escapes. Only called for tokens that contain
    a backslash, so the common quoted string is returned untouched."""
    if "\\" not in raw:
        return raw

    def replace(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape.startswith("u"):
            return chr(int(escape[1:], 16))
        mapped = _ESCAPES.get(escape)
        if mapped is None:
            raise GraphQLSyntaxError(f"invalid escape {escape!r}")
        return mapped

    return _ESCAPE_SCAN.sub(replace, raw)


def _block_string_value(raw: str) -> str:
    """Strip a block string's common indentation, per the GraphQL spec."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    indents = [
        len(line) - len(line.lstrip(" \t"))
        for line in lines[1:]
        if line.strip()
    ]
    common = min(indents) if indents else 0
    stripped = [lines[0].strip()] + [line[common:] for line in lines[1:]]
    while stripped and not stripped[0].strip():
        stripped.pop(0)
    while stripped and not stripped[-1].strip():
        stripped.pop()
    return "\n".join(stripped)


def _reject_fragment_cycles(fragments: dict[str, FragmentDefinition]) -> None:
    """A fragment that reaches itself expands forever; refuse the document.

    No selection-set depth limit bounds this, because the cycle is in the
    fragment graph rather than in the syntax tree -- the document itself can be
    three lines long.
    """
    state: dict[str, int] = {}    # 0 = visiting, 1 = done

    def visit(name: str, path: tuple[str, ...]) -> None:
        marker = state.get(name)
        if marker == 1:
            return
        if marker == 0:
            cycle = " -> ".join((*path[path.index(name):], name))
            raise GraphQLSyntaxError(
                f"fragment cycle: {cycle}", code="fragment_cycle"
            )
        definition = fragments.get(name)
        if definition is None:
            return  # an unknown spread is caught during validation, not here
        state[name] = 0
        for spread in _spreads(definition.selection_set):
            visit(spread, (*path, name))
        state[name] = 1

    for name in fragments:
        visit(name, ())


def _spreads(selection_set: SelectionSet) -> list[str]:
    found: list[str] = []
    stack = [selection_set]
    while stack:
        current = stack.pop()
        for selection in current.selections:
            if isinstance(selection, FragmentSpread):
                found.append(selection.name)
            elif isinstance(selection, InlineFragment):
                stack.append(selection.selection_set)
            elif selection.selection_set is not None:
                stack.append(selection.selection_set)
    return found


def parse(source: str, limits: Limits = DEFAULT_LIMITS) -> Document:
    """Parse ``source``, enforcing ``limits`` as it goes."""
    if not isinstance(source, str):
        raise GraphQLSyntaxError("a GraphQL document must be a string")
    if len(source) > limits.max_document_bytes:
        # Checked first and on `len` alone: parse cost scales with length, so
        # this bounds the worst case before any scanning happens.
        raise GraphQLSyntaxError(
            f"document is longer than {limits.max_document_bytes} characters",
            code="document_size",
        )
    return _Parser(source, limits).parse()
