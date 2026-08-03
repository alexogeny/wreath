"""SCIM filters -- RFC 7644 section 3.4.2.2 -- tokenised, parsed, evaluated.

A filter arrives as a query string an identity provider wrote:

    userName eq "alice@example.com"
    active eq true and (userName sw "a" or userName sw "b")
    emails[type eq "work"].value ew "example.com"

and has to become a predicate over the SCIM *representation* of a resource --
the same JSON object the endpoint would return. Evaluating against the
representation rather than against the store is deliberate: there is exactly one
mapping from wreath's model to SCIM's, it lives in `resources.py`, and a filter
that read the store directly would be a second one that could disagree with the
first about what `active` means.

Three properties this parser is written to hold, each of them a control:

* **An attribute this provider does not hold is refused, not answered.** A
  filter naming `externalId` -- which wreath's user record has nowhere to put --
  cannot be answered truthfully, and answering it "no matches" is how a
  provisioning client concludes the user is absent and creates them again. It
  raises `FilterError` and the endpoint answers 400 `invalidFilter`.
* **Depth and length are bounded.** Both nest without limit in the grammar, and
  an unbounded recursive-descent parser over an attacker-supplied string is a
  stack overflow one authenticated request long.
* **A parse failure is never a match.** Every failure path raises; there is no
  branch that returns a permissive default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_DEPTH",
    "MAX_LENGTH",
    "Compare",
    "Filter",
    "FilterError",
    "Group",
    "Logical",
    "Negate",
    "ValuePath",
    "matches",
    "parse",
    "values_at",
]

#: Longest filter string accepted. Long enough for the `or` chain a directory
#: sends when it reconciles a page of users, short enough that parsing one is
#: not a workload.
MAX_LENGTH = 2048

#: Deepest nesting of `(...)`, `not(...)` and `attr[...]` accepted. The grammar
#: permits unbounded nesting and the parser is recursive, so this is what keeps
#: `((((...))))` from being a stack overflow rather than a 400.
MAX_DEPTH = 16

#: The comparison operators of section 3.4.2.1, less `pr`, which takes no value.
COMPARISON = frozenset({"eq", "ne", "co", "sw", "ew", "gt", "ge", "lt", "le"})

_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

#: Characters that may appear unquoted in a token. `:` and `.` carry the schema
#: URN and the sub-attribute, `$` is there for `$ref`, and `-` and `_` are legal
#: in a SCIM attribute name.
_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:$-+"
)


class FilterError(ValueError):
    """A filter that cannot be parsed, or names something this provider lacks.

    Carries `detail` -- the sentence that reaches the client in the SCIM error
    document. Each construction site writes a *different* one, because a
    refusal test that only asserts the status code passes on whichever branch
    happened to fire.
    """

    __slots__ = ("detail",)

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Compare:
    """`attrPath op value`, or `attrPath pr`.

    `path` is lowercased and stripped of any schema URN, so
    `urn:ietf:params:scim:schemas:core:2.0:User:userName` and `userName` are one
    node. `value` is `None` exactly when `op` is `"pr"`.
    """

    path: str
    op: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class Logical:
    """`left and right`, or `left or right`."""

    op: str
    left: Filter
    right: Filter


@dataclass(frozen=True, slots=True)
class Negate:
    """`not (...)`."""

    operand: Filter


@dataclass(frozen=True, slots=True)
class Group:
    """`(...)`, kept as a node so a round-trip can reproduce the source."""

    operand: Filter


@dataclass(frozen=True, slots=True)
class ValuePath:
    """`attrPath[predicate]` -- true when *any* element of the attribute matches.

    The existential reading is the specification's: a value path selects the
    elements of a multi-valued attribute for which the predicate holds, and as a
    filter it asks whether that selection is non-empty.
    """

    path: str
    predicate: Filter


type Filter = Compare | Logical | Negate | Group | ValuePath


# --- lexer ------------------------------------------------------------------


def _string_token(source: str, start: int) -> tuple[str, int]:
    """Read one double-quoted JSON string beginning at `start`."""
    index = start + 1
    out: list[str] = []
    length = len(source)
    while index < length:
        char = source[index]
        if char == '"':
            return "".join(out), index + 1
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= length:
            raise FilterError("filter ends inside a string escape")
        escape = source[index + 1]
        if escape == "u":
            digits = source[index + 2 : index + 6]
            if len(digits) != 4:
                raise FilterError("filter has a truncated \\u escape")
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                raise FilterError(f"filter has an invalid \\u escape: \\u{digits}") from None
            index += 6
            continue
        replacement = _ESCAPES.get(escape)
        if replacement is None:
            raise FilterError(f"filter has an unknown string escape: \\{escape}")
        out.append(replacement)
        index += 2
    raise FilterError("filter has an unterminated string")


def _number_token(text: str) -> Any:
    """`text` as an int or a float, or `None` when it is not a number."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _tokenize(source: str) -> list[tuple[str, Any]]:
    """`source` as `(kind, value)` pairs; kinds are `(`, `)`, `[`, `]`, word, str."""
    tokens: list[tuple[str, Any]] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char in "()[]":
            tokens.append((char, char))
            index += 1
            continue
        if char == '"':
            text, index = _string_token(source, index)
            tokens.append(("str", text))
            continue
        if char not in _WORD_CHARS:
            raise FilterError(f"filter contains an unexpected character: {char!r}")
        start = index
        while index < length and source[index] in _WORD_CHARS:
            index += 1
        tokens.append(("word", source[start:index]))
    return tokens


# --- parser -----------------------------------------------------------------


def _attribute(word: str) -> str:
    """The attribute path in `word`, lowercased, with any schema URN removed.

    A qualified path is `urn:...:User:userName`, so the attribute is whatever
    follows the last colon -- which also leaves an unqualified `userName`
    untouched, since it has no colon at all.
    """
    return word.rpartition(":")[2].lower()


class _Parser:
    """Recursive descent over the token list, with the depth budget threaded."""

    __slots__ = ("_at", "_tokens")

    def __init__(self, tokens: list[tuple[str, Any]]) -> None:
        self._tokens = tokens
        self._at = 0

    def peek(self) -> tuple[str, Any] | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def take(self) -> tuple[str, Any]:
        token = self.peek()
        if token is None:
            raise FilterError("filter ends where more was expected")
        self._at += 1
        return token

    def expect(self, kind: str) -> tuple[str, Any]:
        token = self.take()
        if token[0] != kind:
            raise FilterError(f"filter expected {kind!r} and found {token[1]!r}")
        return token

    def at_keyword(self, word: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == "word" and token[1].lower() == word

    # or > and > not, loosest first, as section 3.4.2.2 specifies.
    def disjunction(self, depth: int) -> Filter:
        node = self.conjunction(depth)
        while self.at_keyword("or"):
            self.take()
            node = Logical("or", node, self.conjunction(depth))
        return node

    def conjunction(self, depth: int) -> Filter:
        node = self.unary(depth)
        while self.at_keyword("and"):
            self.take()
            node = Logical("and", node, self.unary(depth))
        return node

    def unary(self, depth: int) -> Filter:
        if depth > MAX_DEPTH:
            raise FilterError(
                f"filter nests deeper than {MAX_DEPTH} levels; simplify it or "
                "send several requests"
            )
        if self.at_keyword("not"):
            self.take()
            self.expect("(")
            operand = self.disjunction(depth + 1)
            self.expect(")")
            return Negate(operand)
        token = self.peek()
        if token is not None and token[0] == "(":
            self.take()
            operand = self.disjunction(depth + 1)
            self.expect(")")
            return Group(operand)
        return self.attribute_expression(depth)

    def attribute_expression(self, depth: int) -> Filter:
        kind, word = self.take()
        if kind != "word":
            raise FilterError(f"filter expected an attribute and found {word!r}")
        path = _attribute(word)
        if not path:
            raise FilterError(f"filter has an empty attribute name in {word!r}")
        token = self.peek()
        if token is not None and token[0] == "[":
            self.take()
            predicate = self.disjunction(depth + 1)
            self.expect("]")
            return ValuePath(path, predicate)
        operator_kind, operator = self.take()
        if operator_kind != "word":
            raise FilterError(f"filter expected an operator and found {operator!r}")
        operator = operator.lower()
        if operator == "pr":
            return Compare(path, "pr")
        if operator not in COMPARISON:
            raise FilterError(f"filter has an unknown operator: {operator!r}")
        return Compare(path, operator, self.comparison_value())

    def comparison_value(self) -> Any:
        kind, raw = self.take()
        if kind == "str":
            return raw
        if kind != "word":
            raise FilterError(f"filter expected a value and found {raw!r}")
        lowered = raw.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered == "null":
            return None
        number = _number_token(raw)
        if number is None:
            raise FilterError(
                f"filter has an unquoted value: {raw!r}; strings must be quoted"
            )
        return number


def parse(source: str, *, attributes: frozenset[str] | None = None) -> Filter:
    """`source` as a filter tree, refusing anything this provider cannot answer.

    Args:
        source: the raw `filter` query parameter.
        attributes: the lowercased attribute names the resource type actually
            holds. A filter naming anything else raises rather than evaluating
            to no matches -- see the module docstring for why the difference
            matters to a provisioning client.

    Raises:
        FilterError: the filter is empty, too long, too deeply nested,
            syntactically invalid, or names an attribute outside `attributes`.
    """
    if len(source) > MAX_LENGTH:
        raise FilterError(
            f"filter is longer than {MAX_LENGTH} characters ({len(source)})"
        )
    tokens = _tokenize(source)
    if not tokens:
        raise FilterError("filter is empty")
    parser = _Parser(tokens)
    node = parser.disjunction(0)
    trailing = parser.peek()
    if trailing is not None:
        # `peek()` is the whole condition: unfinished *is* having a token left,
        # so asking `finished()` first and then handling a `None` token would be
        # the same question written twice, with a branch nothing can reach.
        raise FilterError(f"filter has trailing input beginning at {trailing[1]!r}")
    if attributes is not None:
        _check_attributes(node, attributes)
    return node


def _check_attributes(node: Filter, attributes: frozenset[str]) -> None:
    """Raise unless every attribute named in `node` is one we hold."""
    match node:
        case Compare(path=path):
            base = path.partition(".")[0]
            if base not in attributes:
                raise FilterError(
                    f"this provider does not hold an attribute named {base!r}; "
                    f"it holds {', '.join(sorted(attributes))}"
                )
        case ValuePath(path=path):
            # Only the attribute itself is checked. Everything inside the
            # brackets names a *sub*-attribute of one of its elements --
            # `emails[type eq "work"]` -- and those live in a different
            # namespace, so checking them against the top-level set would
            # refuse every correct value path there is.
            if path not in attributes:
                raise FilterError(
                    f"this provider does not hold an attribute named {path!r}; "
                    f"it holds {', '.join(sorted(attributes))}"
                )
        case Logical(left=left, right=right):
            _check_attributes(left, attributes)
            _check_attributes(right, attributes)
        case Negate(operand=operand) | Group(operand=operand):
            _check_attributes(operand, attributes)


# --- evaluation -------------------------------------------------------------


def values_at(resource: Any, path: str) -> list[Any]:
    """Every value `path` names in `resource`, flattening multi-valued steps.

    Attribute names are case-insensitive throughout SCIM, so the walk lowercases
    both sides. A step into a list distributes over its elements, which is what
    makes `emails.value eq "x"` true when *any* email matches -- the reading
    section 3.4.2.2 gives multi-valued attributes.
    """
    current: list[Any] = [resource]
    for step in path.split("."):
        wanted = step.lower()
        found: list[Any] = []
        for item in current:
            if not isinstance(item, Mapping):
                continue
            for key, value in item.items():
                if isinstance(key, str) and key.lower() == wanted:
                    if isinstance(value, list):
                        found.extend(value)
                    else:
                        found.append(value)
        # No early return for an empty `found`: the next step over an empty list
        # produces an empty list anyway, and the guard that says so is a second
        # spelling of the loop's own behaviour.
        current = found
    return current


def _present(value: Any) -> bool:
    """`pr`: a non-null value, and a non-empty one for strings and containers."""
    if value is None:
        return False
    if isinstance(value, str | list | dict | tuple):
        return len(value) > 0
    return True


def _compare(value: Any, op: str, wanted: Any) -> bool:
    """One `attrPath op value` decision against one resolved value.

    Strings compare case-insensitively. SCIM makes that per-attribute
    (`caseExact`), and every attribute this provider publishes is
    `caseExact: false`, so the rule is uniform here rather than a table -- and
    a table with one distinct entry is a table that will be wrong when the
    second entry arrives.
    """
    if op == "eq":
        return _equal(value, wanted)
    if op == "ne":
        return not _equal(value, wanted)
    if op in ("co", "sw", "ew"):
        if not isinstance(value, str) or not isinstance(wanted, str):
            return False
        left, right = value.lower(), wanted.lower()
        if op == "co":
            return right in left
        if op == "sw":
            return left.startswith(right)
        return left.endswith(right)
    ordering = _order(value, wanted)
    if ordering is None:
        return False
    if op == "gt":
        return ordering > 0
    if op == "ge":
        return ordering >= 0
    if op == "lt":
        return ordering < 0
    return ordering <= 0


def _equal(value: Any, wanted: Any) -> bool:
    if isinstance(value, str) and isinstance(wanted, str):
        return value.lower() == wanted.lower()
    if isinstance(value, bool) or isinstance(wanted, bool):
        # `True == 1` in Python and does not in SCIM, so booleans only ever
        # equal booleans here.
        return value is wanted
    if isinstance(value, int | float) and isinstance(wanted, int | float):
        return value == wanted
    return value == wanted


def _order(value: Any, wanted: Any) -> int | None:
    """-1/0/1 for a comparable pair, or `None` when the pair is not ordered."""
    if isinstance(value, str) and isinstance(wanted, str):
        left, right = value.lower(), wanted.lower()
        return (left > right) - (left < right)
    if (
        isinstance(value, int | float)
        and isinstance(wanted, int | float)
        and not isinstance(value, bool)
        and not isinstance(wanted, bool)
    ):
        return (value > wanted) - (value < wanted)
    return None


def matches(node: Filter, resource: Any) -> bool:
    """Does `resource` -- a SCIM representation -- satisfy `node`?"""
    match node:
        case Compare(path=path, op="pr"):
            return any(_present(value) for value in values_at(resource, path))
        case Compare(path=path, op=op, value=wanted):
            return any(_compare(value, op, wanted) for value in values_at(resource, path))
        case ValuePath(path=path, predicate=predicate):
            return any(
                matches(predicate, element) for element in values_at(resource, path)
            )
        case Logical(op="and", left=left, right=right):
            return matches(left, resource) and matches(right, resource)
        case Logical(left=left, right=right):
            return matches(left, resource) or matches(right, resource)
        case Negate(operand=operand):
            return not matches(operand, resource)
        case Group(operand=operand):
            return matches(operand, resource)
    raise FilterError("filter contains a node this evaluator does not know")
