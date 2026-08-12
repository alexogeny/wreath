"""The built-in Cedar policy engine: parse and compile once, evaluate native.

This module implements the Cedar policy language core with no dependencies:
the policy grammar (`permit`/`forbid`, scope constraints, `when`/
`unless` conditions), the strict expression language, the entity hierarchy,
and the authorization algorithm (forbid overrides permit; default deny; an
erroring policy is skipped and reported, never silently satisfied).

The split follows Wreath's usual shape. Parsing and compilation happen here,
in Python, exactly once — at application startup, where a syntax error is an
application bug and not a request-time surprise. The compiled program is a
flat tuple tape, and the per-request evaluator that walks it is C
(`wreath._native._core.cedar_is_authorized`).

Scope is deliberate and loud. The Cedar core — everything above — is
implemented faithfully. Extension types (`ip`, `decimal`, `datetime`)
and schema-based validation are not implemented yet; policies that use them
fail at parse time with a clear error rather than evaluating differently from
real Cedar. A policy set that parses is a policy set this engine evaluates by
the book.

The public surface is three names, re-exported from
`wreath.authorization`:

- `EntityUid` — a typed entity reference, `User::"alice"`.
- `CedarEntity` — one entity: uid, attributes, parents.
- `CedarPolicies` — a parsed policy set that is itself an engine: it
  satisfies the `CedarEngine` protocol, so
  `CedarAuthorizer(engine=CedarPolicies(source))` needs nothing else.

Compiled value model consumed by the evaluator: `bool`, i64 `int`, `str`, an
entity uid as a `(type, id)` string 2-tuple, a set as a
duplicate-free `list`, and a record as a `dict`. Booleans are never
integers — every check tests `bool` first, because Python's bool subclasses
int and Cedar's type system does not.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .._native import _core
from .models import AuthorizationDecision

__all__ = [
    "CedarEntity",
    "CedarParseError",
    "CedarPolicies",
    "EntityUid",
]

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

# Expression opcodes; `wreath/_native/cedar.c` switches on these.
_OP_CONST = 0
_OP_VAR = 1
_OP_AND = 2
_OP_OR = 3
_OP_NOT = 4
_OP_ARITH = 5
_OP_CMP = 6
_OP_EQ = 7
_OP_NE = 8
_OP_IN = 9
_OP_HAS = 10
_OP_LIKE = 11
_OP_IS = 12
_OP_IF = 13
_OP_SET = 14
_OP_RECORD = 15
_OP_GETATTR = 16
_OP_METHOD = 17

_VARS = {"principal": 0, "action": 1, "resource": 2, "context": 3}
_ARITH_OPS = {"+": 0, "-": 1, "*": 2}
_CMP_OPS = {"<": 0, "<=": 1, ">": 2, ">=": 3}
_METHOD_IDS = {"contains": 0, "containsAll": 1, "containsAny": 2, "isEmpty": 3}

# Scope kinds: (0,) any; (1, uid) ==; (2, uid) in; (3, uids) in [..]; (4, type, uid|None) is.
_SCOPE_ANY = 0
_SCOPE_EQ = 1
_SCOPE_IN = 2
_SCOPE_IN_SET = 3
_SCOPE_IS = 4


def _context_attribute(name: str) -> tuple[Any, ...]:
    """The compiled shape of `context.<name>`, which is what a set test reads."""
    return (_OP_GETATTR, (_OP_VAR, _VARS["context"]), name)


#: Set methods whose argument names members. `isEmpty` takes none and names none.
_NAMING_METHODS = frozenset({_METHOD_IDS["contains"], _METHOD_IDS["containsAll"],
                             _METHOD_IDS["containsAny"]})


def _literal_names(node: object) -> Iterator[str]:
    """The string literals in a naming method's argument.

    `contains("a")` is one constant; `containsAll(["a", "b"])` is a set literal
    of them. A non-literal argument yields nothing -- absence of evidence, not
    evidence that no name is given, which is why the caller treats an empty
    result as "cannot validate" rather than "references nothing".
    """
    if not isinstance(node, tuple) or not node:
        return
    if node[0] == _OP_CONST and len(node) == 2 and isinstance(node[1], str):
        yield node[1]
    elif node[0] == _OP_SET and isinstance(node[1], tuple | list):
        for element in node[1]:
            yield from _literal_names(element)


def _reads_context(policies: Iterable[Any], attribute: str) -> bool:
    """Whether any policy reads `context.<attribute>` at all, in any shape.

    A presence test rather than a member walk, and the two are genuinely
    different questions: `_referenced_members` answers *which names* a set key
    is tested against, and cannot distinguish "no policy reads this" from "read
    in an unknowable shape" without the caller decoding `frozenset()` versus
    `None`. This answers the plain question a scalar key needs -- `context.actor`
    and `context.delegated` are not sets and have no members to walk.

    Its one caller uses it to decide whether a delegated request needs a
    *second* evaluation. Getting it wrong in the false direction skips that
    evaluation and would let a delegate exceed its delegator, so this walks the
    whole tree and tests for the compiled attribute read itself rather than for
    any particular expression shape around it.
    """
    target = _context_attribute(attribute)
    stack: list[Any] = list(policies)
    while stack:
        node = stack.pop()
        if not isinstance(node, tuple | list):
            continue
        if isinstance(node, tuple) and target in node:
            return True
        stack.extend(node)
    return False


def _context_attributes(policies: Iterable[Any]) -> frozenset[str]:
    """Every direct ``context.<attribute>`` read in these compiled policies."""
    context = (_OP_VAR, _VARS["context"])
    found: set[str] = set()
    stack: list[Any] = list(policies)
    while stack:
        node = stack.pop()
        if not isinstance(node, tuple | list):
            continue
        if len(node) == 3 and node[1] == context:
            found.add(node[2])
        stack.extend(node)
    return frozenset(found)


def _referenced_members(
    policies: Iterable[Any], attribute: str
) -> frozenset[str] | None:
    """Every literal name tested against `context.<attribute>`, or None.

    One walk for every set-valued context key the authorizer resolves lazily —
    `flags` and `regions` today. They ask an identical question ("which members
    does the policy set actually name?") for an identical reason (resolve those
    and no more), so they are one implementation parameterised by the attribute
    rather than two that must be kept in step.

    A generic walk over the expression tuples rather than a shape-by-shape
    visitor: such a test is legal anywhere an expression is, including nested
    inside `if`, `&&` and a set literal, and a visitor that knew only the
    top-level shapes would silently miss half the policy set.

    **`None` means "resolve every member".** The caller resolves only the names
    listed here, which is exact while every reference is a literal `contains`,
    `containsAll` or `containsAny`. Two shapes break that: `isEmpty()` names no
    member but its answer depends on all of them, and a computed argument names
    one this walk cannot know. Either makes the list incomplete rather than
    short, so it is withheld entirely -- an optimisation that changes an
    authorization answer is a defect, and a partial list would.
    """
    target = _context_attribute(attribute)
    found: set[str] = set()
    stack: list[Any] = list(policies)
    while stack:
        node = stack.pop()
        if not isinstance(node, tuple | list):
            continue
        if isinstance(node, tuple) and target in node:
            # A read of `context.<attribute>`. Enumerable only as the target of
            # one of the naming methods, with a literal argument.
            if (
                len(node) == 4
                and node[0] == _OP_METHOD
                and node[1] in _NAMING_METHODS
                and node[2] == target
            ):
                names = frozenset(_literal_names(node[3]))
                if not names:
                    return None  # a computed argument
                found.update(names)
            else:
                return None  # isEmpty(), a comparison, or passed along whole
        stack.extend(node)
    return frozenset(found)


class CedarParseError(ValueError):
    """A policy set (or entity reference) that is not valid Cedar."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        if line is not None:
            message = f"{message} (line {line}, column {column})"
        super().__init__(message)
        self.line = line
        self.column = column


# -- public value types -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityUid:
    """A Cedar entity reference: a type path and an id, `User::"alice"`."""

    type: str
    id: str

    def __str__(self) -> str:
        escaped = self.id.replace("\\", "\\\\").replace('"', '\\"')
        return f'{self.type}::"{escaped}"'

    @classmethod
    def parse(cls, text: str) -> EntityUid:
        """Parse `Type::"id"` (Cedar syntax) or the bare `Type::id` form."""
        try:
            tokens = _tokenize(text)
        except CedarParseError:
            tokens = []
        if tokens:
            parser = _Parser(tokens)
            try:
                uid = parser._entity_uid()
                parser._expect("eof")
                return uid
            except CedarParseError:
                pass
        head, sep, tail = text.rpartition("::")
        if not sep or not head or not tail:
            raise CedarParseError(f'{text!r} is not an entity reference; expected Type::"id"')
        return cls(head, tail)


@dataclass(frozen=True, slots=True)
class CedarEntity:
    """One entity: its uid, its attributes, and its parents in the hierarchy."""

    uid: EntityUid
    attrs: Mapping[str, object] = field(default_factory=dict)
    parents: tuple[EntityUid, ...] = ()


# -- value conversion into the compiled model ---------------------------------


def _to_cedar_value(value: object, *, where: str) -> Any:
    """Convert one complete value graph into the evaluator's value model."""
    return _core.cedar_to_value(value, EntityUid, Mapping, where)


# -- lexer --------------------------------------------------------------------

_KEYWORDS = frozenset(
    {
        "permit", "forbid", "when", "unless", "principal", "action", "resource",
        "context", "true", "false", "if", "then", "else", "in", "like", "has", "is",
    }
)
_PUNCTUATION = (
    "::", "||", "&&", "==", "!=", "<=", ">=",
    "(", ")", "[", "]", "{", "}", ",", ";", ":", ".", "@", "!", "<", ">", "+", "-", "*",
)


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # "ident", "keyword", "int", "string", "eof", or the punctuation itself
    value: str
    line: int
    column: int


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    index, line, line_start = 0, 1, 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in " \t\r":
            index += 1
            continue
        if char == "\n":
            index += 1
            line += 1
            line_start = index
            continue
        column = index - line_start + 1
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if char == '"':
            value, index = _lex_string(source, index, line, column)
            tokens.append(_Token("string", value, line, column))
            continue
        if char.isdigit():
            start = index
            while index < length and source[index].isdigit():
                index += 1
            tokens.append(_Token("int", source[start:index], line, column))
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < length and (source[index].isalnum() or source[index] == "_"):
                index += 1
            word = source[start:index]
            tokens.append(_Token("keyword" if word in _KEYWORDS else "ident", word, line, column))
            continue
        for punctuation in _PUNCTUATION:
            if source.startswith(punctuation, index):
                tokens.append(_Token(punctuation, punctuation, line, column))
                index += len(punctuation)
                break
        else:
            raise CedarParseError(f"unexpected character {char!r}", line, column)
    tokens.append(_Token("eof", "", line, length - line_start + 1))
    return tokens


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", '"': '"', "'": "'", "\\": "\\", "*": "\\*"}


def _lex_string(source: str, index: int, line: int, column: int) -> tuple[str, int]:
    # `\*` survives as the two-character sequence so `like` patterns can
    # tell an escaped literal star from a wildcard; every other escape decodes.
    parts: list[str] = []
    index += 1
    while index < len(source):
        char = source[index]
        if char == '"':
            return "".join(parts), index + 1
        if char == "\n":
            break
        if char == "\\":
            index += 1
            if index >= len(source):
                break
            escape = source[index]
            if escape == "u" and source[index + 1 : index + 2] == "{":
                closing = source.find("}", index + 2)
                if closing < 0:
                    raise CedarParseError("unterminated \\u{...} escape", line, column)
                parts.append(chr(int(source[index + 2 : closing], 16)))
                index = closing + 1
                continue
            decoded = _ESCAPES.get(escape)
            if decoded is None:
                raise CedarParseError(f"unknown string escape \\{escape}", line, column)
            parts.append(decoded)
            index += 1
            continue
        parts.append(char)
        index += 1
    raise CedarParseError("unterminated string literal", line, column)


def _unescape_star(text: str) -> str:
    """Decode the `\\*` sequence the lexer preserves for `like` patterns."""
    return text.replace("\\*", "*")


def _pattern_segments(pattern: str) -> tuple[str, ...]:
    """Split a `like` pattern on wildcards; `\\*` stays a literal star.

    A single segment means no wildcard (exact match). Otherwise the first
    segment anchors the start, the last anchors the end, and the middle
    segments must appear in order between them.
    """
    segments: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("\\*", index):
            current.append("*")
            index += 2
            continue
        if pattern[index] == "*":
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(pattern[index])
        index += 1
    segments.append("".join(current))
    return tuple(segments)


# -- parser: source text straight to the compiled program ---------------------

_METHODS = frozenset(_METHOD_IDS)
_EXTENSIONS = frozenset({"ip", "decimal", "datetime", "duration", "offset"})


class _Parser:
    """Parses Cedar text directly into the tuple program both evaluators run."""

    __slots__ = ("_index", "_tokens")

    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._index = 0

    # basic machinery

    def _peek(self, offset: int = 0) -> _Token:
        return self._tokens[min(self._index + offset, len(self._tokens) - 1)]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        if token.kind != "eof":
            self._index += 1
        return token

    def _match(self, kind: str, value: str | None = None) -> _Token | None:
        token = self._peek()
        if token.kind == kind and (value is None or token.value == value):
            return self._advance()
        return None

    def _expect(self, kind: str, value: str | None = None) -> _Token:
        token = self._match(kind, value)
        if token is None:
            actual = self._peek()
            wanted = value or kind
            raise CedarParseError(
                f"expected {wanted!r}, found {actual.value or actual.kind!r}",
                actual.line,
                actual.column,
            )
        return token

    def _fail(self, message: str) -> CedarParseError:
        token = self._peek()
        return CedarParseError(message, token.line, token.column)

    # policies

    def policies(self) -> tuple[tuple, ...]:
        parsed: list[tuple] = []
        while self._peek().kind != "eof":
            parsed.append(self._policy(len(parsed)))
        if not parsed:
            raise CedarParseError("policy set is empty")
        return tuple(parsed)

    def _policy(self, index: int) -> tuple:
        annotations: dict[str, str] = {}
        while self._match("@"):
            name = self._expect("ident").value
            self._expect("(")
            annotations[name] = _unescape_star(self._expect("string").value)
            self._expect(")")
        effect_token = self._peek()
        if effect_token.kind == "keyword" and effect_token.value in ("permit", "forbid"):
            self._advance()
        else:
            raise self._fail("expected 'permit' or 'forbid'")
        self._expect("(")
        principal = self._scope("principal", allow_is=True)
        self._expect(",")
        action = self._scope("action", allow_is=False)
        self._expect(",")
        resource = self._scope("resource", allow_is=True)
        self._expect(")")
        conditions: list[tuple[int, object]] = []
        while True:
            token = self._peek()
            if token.kind == "keyword" and token.value in ("when", "unless"):
                self._advance()
                self._expect("{")
                conditions.append((int(token.value == "unless"), self._expression()))
                self._expect("}")
                continue
            break
        self._expect(";")
        return (
            int(effect_token.value == "forbid"),
            annotations.get("id", f"policy{index}"),
            principal,
            action,
            resource,
            tuple(conditions),
        )

    def _scope(self, name: str, *, allow_is: bool) -> tuple:
        self._expect("keyword", name)
        if self._match("=="):
            return (_SCOPE_EQ, self._uid_tuple())
        if self._match("keyword", "in"):
            if name == "action" and self._match("["):
                uids: list[tuple[str, str]] = [self._uid_tuple()]
                while self._match(","):
                    uids.append(self._uid_tuple())
                self._expect("]")
                return (_SCOPE_IN_SET, frozenset(uids))
            return (_SCOPE_IN, self._uid_tuple())
        if self._peek().kind == "keyword" and self._peek().value == "is":
            if not allow_is:
                raise self._fail(f"'is' is not allowed in the {name} scope")
            self._advance()
            entity_type = self._type_path()
            ancestor = self._uid_tuple() if self._match("keyword", "in") else None
            return (_SCOPE_IS, entity_type, ancestor)
        return (_SCOPE_ANY,)

    def _type_path(self) -> str:
        segments = [self._expect("ident").value]
        while self._peek().kind == "::" and self._peek(1).kind == "ident":
            self._advance()
            segments.append(self._expect("ident").value)
        return "::".join(segments)

    def _entity_uid(self) -> EntityUid:
        entity_type = self._type_path()
        self._expect("::")
        return EntityUid(entity_type, _unescape_star(self._expect("string").value))

    def _uid_tuple(self) -> tuple[str, str]:
        uid = self._entity_uid()
        return (uid.type, uid.id)

    # expressions, loosest binding first

    def _expression(self) -> object:
        if self._peek().kind == "keyword" and self._peek().value == "if":
            self._advance()
            condition = self._expression()
            self._expect("keyword", "then")
            then = self._expression()
            self._expect("keyword", "else")
            return (_OP_IF, condition, then, self._expression())
        return self._or()

    def _or(self) -> object:
        left = self._and()
        while self._match("||"):
            left = (_OP_OR, left, self._and())
        return left

    def _and(self) -> object:
        left = self._relation()
        while self._match("&&"):
            left = (_OP_AND, left, self._relation())
        return left

    def _relation(self) -> object:
        left = self._additive()
        token = self._peek()
        if token.kind in ("==", "!="):
            self._advance()
            return (_OP_EQ if token.kind == "==" else _OP_NE, left, self._additive())
        if token.kind in _CMP_OPS:
            self._advance()
            return (_OP_CMP, _CMP_OPS[token.kind], left, self._additive())
        if token.kind == "keyword" and token.value == "in":
            self._advance()
            return (_OP_IN, left, self._additive())
        if token.kind == "keyword" and token.value == "has":
            self._advance()
            return (_OP_HAS, left, self._attribute_name())
        if token.kind == "keyword" and token.value == "like":
            self._advance()
            return (_OP_LIKE, left, _pattern_segments(self._expect("string").value))
        if token.kind == "keyword" and token.value == "is":
            self._advance()
            entity_type = self._type_path()
            ancestor = self._additive() if self._match("keyword", "in") else None
            return (_OP_IS, left, entity_type, ancestor)
        return left

    def _additive(self) -> object:
        left = self._multiplicative()
        while True:
            token = self._peek()
            if token.kind in ("+", "-"):
                self._advance()
                left = (_OP_ARITH, _ARITH_OPS[token.kind], left, self._multiplicative())
                continue
            return left

    def _multiplicative(self) -> object:
        left = self._unary()
        while self._match("*"):
            left = (_OP_ARITH, _ARITH_OPS["*"], left, self._unary())
        return left

    def _unary(self) -> object:
        if self._match("!"):
            return (_OP_NOT, self._unary())
        if self._match("-"):
            operand = self._unary()
            if (
                isinstance(operand, tuple)
                and operand[0] == _OP_CONST
                and type(operand[1]) is int
            ):
                folded = -operand[1]
                if folded < _I64_MIN:
                    raise self._fail("integer literal does not fit in i64")
                return (_OP_CONST, folded)
            return (_OP_ARITH, _ARITH_OPS["-"], (_OP_CONST, 0), operand)
        return self._member()

    def _member(self) -> object:
        expression = self._primary()
        while True:
            if self._match("."):
                name_token = self._peek()
                if name_token.kind not in ("ident", "keyword"):
                    raise self._fail("expected an attribute or method name after '.'")
                self._advance()
                name = name_token.value
                if self._peek().kind == "(":
                    if name not in _METHODS:
                        raise self._fail(
                            f"unknown method .{name}(); the engine supports "
                            "contains, containsAll, containsAny, and isEmpty"
                        )
                    self._advance()
                    arguments: list[object] = []
                    if self._peek().kind != ")":
                        arguments.append(self._expression())
                        while self._match(","):
                            arguments.append(self._expression())
                    self._expect(")")
                    expected = 0 if name == "isEmpty" else 1
                    if len(arguments) != expected:
                        raise self._fail(f".{name}() takes {expected} argument(s)")
                    argument = arguments[0] if arguments else None
                    expression = (_OP_METHOD, _METHOD_IDS[name], expression, argument)
                    continue
                expression = (_OP_GETATTR, expression, name)
                continue
            if self._peek().kind == "[" and self._peek(1).kind == "string":
                self._advance()
                attribute = _unescape_star(self._expect("string").value)
                self._expect("]")
                expression = (_OP_GETATTR, expression, attribute)
                continue
            return expression

    def _attribute_name(self) -> str:
        token = self._peek()
        if token.kind == "ident":
            return self._advance().value
        if token.kind == "string":
            return _unescape_star(self._advance().value)
        raise self._fail("expected an attribute name (identifier or string)")

    def _primary(self) -> object:
        token = self._peek()
        if token.kind == "int":
            self._advance()
            value = int(token.value)
            if value > _I64_MAX:
                raise CedarParseError(
                    "integer literal does not fit in i64", token.line, token.column
                )
            return (_OP_CONST, value)
        if token.kind == "string":
            self._advance()
            return (_OP_CONST, _unescape_star(token.value))
        if token.kind == "keyword":
            if token.value in ("true", "false"):
                self._advance()
                return (_OP_CONST, token.value == "true")
            if token.value in _VARS:
                self._advance()
                return (_OP_VAR, _VARS[token.value])
            raise self._fail(f"unexpected keyword {token.value!r}")
        if token.kind == "ident":
            if token.value in _EXTENSIONS and self._peek(1).kind == "(":
                raise self._fail(
                    f"extension function {token.value}() is not supported by the "
                    "built-in engine yet"
                )
            if self._peek(1).kind == "::":
                return (_OP_CONST, self._uid_tuple())
            raise self._fail(f"unknown identifier {token.value!r}")
        if self._match("("):
            expression = self._expression()
            self._expect(")")
            return expression
        if self._match("["):
            items: list[object] = []
            if self._peek().kind != "]":
                items.append(self._expression())
                while self._match(","):
                    items.append(self._expression())
            self._expect("]")
            return (_OP_SET, tuple(items))
        if self._match("{"):
            entries: list[tuple[str, object]] = []
            keys: set[str] = set()
            if self._peek().kind != "}":
                entries.append(self._record_entry(keys))
                while self._match(","):
                    entries.append(self._record_entry(keys))
            self._expect("}")
            return (_OP_RECORD, tuple(entries))
        raise self._fail(f"unexpected token {token.value or token.kind!r}")

    def _record_entry(self, keys: set[str]) -> tuple[str, object]:
        token = self._peek()
        if token.kind not in ("ident", "string", "keyword"):
            raise self._fail("expected a record key")
        self._advance()
        key = _unescape_star(token.value) if token.kind == "string" else token.value
        if key in keys:
            raise self._fail(f"duplicate record key {key!r}")
        keys.add(key)
        self._expect(":")
        return (key, self._expression())


# -- the entity store ---------------------------------------------------------


#: One entry of the evaluator's store: an entity's converted attributes and the
#: transitive closure of its ancestors.
_Uid = tuple[str, str]
_StoreEntry = tuple[dict[str, object], frozenset[_Uid]]
_Store = dict[_Uid, _StoreEntry]
_Parents = dict[_Uid, tuple[_Uid, ...]]


def _entity_maps(
    entities: Iterable[CedarEntity],
) -> tuple[dict[_Uid, dict[str, object]], _Parents]:
    """Validate `entities` and split them into converted attrs and parent uids.

    A later entity with the same uid replaces an earlier one, which is what
    makes per-request entities able to override the static hierarchy.
    """
    attrs: dict[_Uid, dict[str, object]] = {}
    parents: _Parents = {}
    for entity in entities:
        if not isinstance(entity, CedarEntity):
            raise TypeError(
                f"entities must be CedarEntity instances, got {type(entity).__name__!r}"
            )
        uid = (entity.uid.type, entity.uid.id)
        where = f"entity {entity.uid}"
        attrs[uid] = _to_cedar_value(dict(entity.attrs), where=where)
        parent_uids = []
        for parent in entity.parents:
            if not isinstance(parent, EntityUid):
                raise TypeError(f"{where}: parents must be EntityUid instances")
            parent_uids.append((parent.type, parent.id))
        parents[uid] = tuple(parent_uids)
    return attrs, parents


def _ancestors(
    uid: _Uid, parents: Mapping[_Uid, tuple[_Uid, ...]], base: _Store
) -> frozenset[_Uid]:
    """The transitive ancestors of `uid`, walking `parents`.

    `base` is a store whose closures are already complete: reaching one of its
    entities contributes that entity's whole ancestor set in a single step
    instead of continuing the walk. With an empty `base` this is a plain
    fixed-point closure over `parents`; with the static store as `base` it is
    the same result reached without re-walking the static hierarchy.

    A parent that appears in neither map contributes itself and nothing more,
    so a dangling reference is a fact about the hierarchy rather than an error.
    Cycles terminate: an entity is never its own ancestor unless a cycle makes
    it one, which is what a fixed-point closure means.
    """
    seen: set[_Uid] = set()
    frontier = list(parents.get(uid, ()))
    while frontier:
        parent = frontier.pop()
        if parent in seen:
            continue
        seen.add(parent)
        closed = base.get(parent)
        if closed is not None:
            seen |= closed[1]
        else:
            frontier.extend(parents.get(parent, ()))
    return frozenset(seen)


def _build_store(entities: Iterable[CedarEntity]) -> _Store:
    """Compile entities into the evaluator's store: uid -> (attrs, ancestors).

    Ancestors are the *transitive* closure, precomputed here so neither
    evaluator walks the hierarchy while evaluating — `in` is one set membership
    test.

    This is the whole-hierarchy build, run once per policy set at construction.
    `CedarPolicies.is_authorized` does **not** call it for ordinary per-request
    entities; see `_layer_store`, and the note on that function for why.
    """
    attrs, parents = _entity_maps(entities)
    return {uid: (values, _ancestors(uid, parents, {})) for uid, values in attrs.items()}


def _layer_store(
    base: _Store,
    dangling: frozenset[_Uid],
    static: tuple[CedarEntity, ...],
    entities: tuple[CedarEntity, ...],
) -> _Store:
    """`base` with `entities` layered over it, reusing `base`'s closures.

    Row-level authorization calls `is_authorized(entities=...)` once per row
    against an unchanging static hierarchy, so rebuilding that hierarchy each
    time costs `rows x O(hierarchy)` — measured at 385-389us for 400 static
    entities against a 3.6us baseline, and quadratic rather than linear when
    the hierarchy is a chain. Layering pays only for the entities the caller
    actually supplied.

    Reuse is only sound when the request entities cannot change a *static*
    entity's closure, which takes two conditions:

    * **No uid collision.** A request entity sharing a static uid replaces its
      parents, so any static descendant's closure may change.
    * **No dangling completion.** `dangling` is the set of uids the static
      entities name as parents but do not define. Defining one now extends the
      closure of every static entity above it.

    Either one falls back to the full rebuild, which is always correct. The
    conditions are two set intersections against sets computed once, so the
    check costs nothing on the path that skips it.
    """
    attrs, parents = _entity_maps(entities)
    supplied = attrs.keys()
    if not supplied.isdisjoint(base) or not supplied.isdisjoint(dangling):
        return _build_store(static + entities)
    store = dict(base)
    for uid, values in attrs.items():
        store[uid] = (values, _ancestors(uid, parents, base))
    return store


# -- the engine ---------------------------------------------------------------


class CedarPolicies:
    """A parsed Cedar policy set that acts as the authorization engine.

    Parsing happens once, at construction — a syntax error is an application
    bug and surfaces at startup, never during a request. `entities` given
    here are the static hierarchy (roles, groups, resource ownership); the
    per-request entities handed to `is_authorized` are merged over them.
    """

    __slots__ = (
        "_context_by_action",
        "_context_default",
        "_dangling",
        "_entities",
        "_policies",
        "_source",
        "_store",
    )

    def __init__(self, source: str, *, entities: Iterable[CedarEntity] = ()) -> None:
        if not isinstance(source, str) or not source.strip():
            raise CedarParseError("policy source must be non-empty Cedar text")
        self._source = source
        self._policies = _Parser(_tokenize(source)).policies()
        general = []
        exact: dict[tuple[str, str], list[object]] = {}
        for policy in self._policies:
            action_scope = policy[3]
            if action_scope[0] == _SCOPE_EQ:
                exact.setdefault(action_scope[1], []).append(policy)
            else:
                general.append(policy)
        self._context_default = _context_attributes(general)
        self._context_by_action = {
            action: self._context_default | _context_attributes(policies)
            for action, policies in exact.items()
        }
        self._entities = tuple(entities)
        self._store = _build_store(self._entities)
        # Parent uids the static hierarchy names but does not define. A
        # per-request entity that fills one of these changes what the static
        # entities above it can reach, which is one of the two conditions that
        # make the layered store unsound -- see `_layer_store`.
        self._dangling = frozenset(
            parent
            for _, ancestors in self._store.values()
            for parent in ancestors
            if parent not in self._store
        )

    def __len__(self) -> int:
        return len(self._policies)

    def __repr__(self) -> str:
        return f"<CedarPolicies policies={len(self._policies)}>"

    @property
    def source(self) -> str:
        """The Cedar text this policy set was parsed from.

        Public because it identifies the policy set by content, and callers
        that cache a decision against it need a tag that is the same on every
        worker and across a restart. `id()` is not that: CPython reuses
        addresses, so an address-derived tag can survive a reload that replaced
        the policies. Read-only — the parse happens once, in `__init__`, and
        a settable source would let the text drift from `_policies`.
        """
        return self._source

    def referenced_flags(self) -> frozenset[str] | None:
        """Every feature-flag name this policy set tests, or None for "all of them".

        Two jobs. It is the vocabulary a `CedarAuthorizer` validates its flag
        provider against at startup, so `context.flags.contains("new_iu")` fails
        where it is written rather than denying quietly forever; and it is the
        set the authorizer resolves per request, which measured at a fifth of
        the cost of resolving every configured flag.

        `None` means the policy set reads `context.flags` in a shape whose names
        are not statically knowable -- `isEmpty()`, or a computed argument -- so
        the caller must resolve them all. An empty set means no policy reads
        flags at all, which is the overwhelmingly common case and costs nothing.

        Optional capability, probed with `getattr` the way `source` is — an
        outside `CedarEngine` that cannot answer simply gets the safe path.
        """
        return _referenced_members(self._policies, "flags")

    def referenced_regions(self) -> frozenset[str] | None:
        """Every geofence region name this policy set tests, or None for "all".

        The geospatial counterpart of `referenced_flags`, and the same two jobs: the
        vocabulary a `CedarAuthorizer` validates its region set against at
        startup, and the names it resolves per request.

        The difference is what the work costs. A flag is a hash and a
        comparison; a region is a great-circle distance against the caller's
        position, so a policy naming two of a hundred regions does two of them.
        `context.regions.contains(resource.reserve)` — the shape a geofence is
        most naturally written in — has a *computed* argument, so this answers
        `None` and every region is resolved. That is the honest answer rather
        than a short one, and it is why a region set is worth keeping small.
        """
        return _referenced_members(self._policies, "regions")

    def referenced_organizations(self) -> frozenset[str] | None:
        """Every organisation id this policy set tests, or None for "all".

        Read for one purpose only, and it is not validation: an organisation id
        is a **row**, not a declared vocabulary, so refusing to boot because a
        policy names an organisation nobody has created yet would be wrong.
        What it decides is whether membership is resolved at all -- an empty
        answer means no policy reads `context.organizations`, so the caller skips
        the lookup entirely and the fact costs nothing.
        """
        return _referenced_members(self._policies, "organizations")

    def referenced_org_roles(self) -> frozenset[str] | None:
        """Every role-within-an-organisation this policy set tests, or None.

        Unlike organisation ids these *are* a declared vocabulary, so a policy
        naming `"acme:admni"` is refused at startup. The qualified form carries
        an organisation id that cannot be enumerated, so validation checks the
        role half against the declared roles and lets the organisation half
        through -- see `Memberships.names`.
        """
        return _referenced_members(self._policies, "org_roles")

    def referenced_entitlements(self) -> frozenset[str] | None:
        """Every entitlement this policy set tests, or None for "all"."""
        return _referenced_members(self._policies, "entitlements")

    def referenced_quota(self) -> frozenset[str] | None:
        """Every quota state this policy set tests, or None for "all"."""
        return _referenced_members(self._policies, "quota")

    def reads_context(self, attribute: str) -> bool:
        """Whether any policy reads `context.<attribute>`, in any shape.

        The scalar counterpart to the `referenced_*` walks. `CedarAuthorizer`
        asks it about `delegated` and `actor` to decide whether a delegated
        request needs a second evaluation, which is a question about presence
        rather than about members.
        """
        return _reads_context(self._policies, attribute)

    def context_attributes_for_action(self, action: object) -> frozenset[str]:
        """Context keys reachable by policies that can match this action.

        Only a different exact-equality action scope proves irrelevance here.
        Hierarchical and set scopes remain in the candidate set because request
        entities can make them match even when their literal target differs.
        """
        action_uid = _as_uid_tuple(action, "action")
        return self._context_by_action.get(action_uid, self._context_default)

    def is_authorized(
        self,
        *,
        principal: object,
        action: object,
        resource: object,
        context: Mapping[str, object] | None = None,
        entities: object = None,
    ) -> AuthorizationDecision:
        """Evaluate this policy set for one request. Keyword arguments only.

        `principal`, `action` and `resource` are each an `EntityUid` or a
        `Type::"id"` string; anything else is a `TypeError`. `context` is a
        mapping converted into the compiled value model, and `entities` is a
        `CedarEntity` or an iterable of them, layered over the static hierarchy
        given to the constructor — a request entity sharing a static uid
        replaces it.

        The decision follows Cedar: forbid overrides permit, the default is
        deny, and a policy that errors while evaluating is skipped and named in
        `diagnostics` rather than counting as satisfied. `reason` says which of
        the three it was — `"explicit forbid"`, `"cedar permit"`, or
        `"no permit policy matched"` — and `diagnostics` names every policy that
        matched or was skipped, and why. `reason` names no policy, which matters
        because it is what reaches the client as the 403's `detail`; the policy
        ids stay in `diagnostics`, which is not sent.

        Evaluation runs in C. This does no parsing — that happened once, in
        `__init__`.
        """
        request_entities = _as_entities(entities)
        if request_entities:
            store = _layer_store(
                self._store, self._dangling, self._entities, request_entities
            )
        else:
            store = self._store
        allowed, reason, diagnostics = _core.cedar_is_authorized(
            self._policies,
            _as_uid_tuple(principal, "principal"),
            _as_uid_tuple(action, "action"),
            _as_uid_tuple(resource, "resource"),
            _to_cedar_value(dict(context or {}), where="context"),
            store,
        )
        return AuthorizationDecision(allowed, reason, diagnostics)

    def _is_authorized_many(
        self,
        *,
        principal: object,
        action: object,
        resources: tuple[object, ...],
        context: Mapping[str, object] | None,
        entities: object,
        stop_on_denied: bool,
    ) -> tuple[AuthorizationDecision, ...]:
        """Evaluate several resources against one compiled request context."""
        request_entities = _as_entities(entities)
        if request_entities:
            store = _layer_store(
                self._store, self._dangling, self._entities, request_entities
            )
        else:
            store = self._store
        principal_uid = _as_uid_tuple(principal, "principal")
        action_uid = _as_uid_tuple(action, "action")
        compiled_context = _to_cedar_value(dict(context or {}), where="context")
        results = _core.cedar_is_authorized_many(
            self._policies,
            principal_uid,
            action_uid,
            tuple(_as_uid_tuple(resource, "resource") for resource in resources),
            compiled_context,
            store,
            stop_on_denied,
        )
        return tuple(AuthorizationDecision(*result) for result in results)


def _as_uid_tuple(value: object, name: str) -> tuple[str, str]:
    if isinstance(value, EntityUid):
        return (value.type, value.id)
    if isinstance(value, str):
        uid = EntityUid.parse(value)
        return (uid.type, uid.id)
    raise TypeError(
        f"{name} must be an EntityUid or a 'Type::\"id\"' string, got {type(value).__name__!r}"
    )


def _as_entities(value: object) -> tuple[CedarEntity, ...]:
    if value is None:
        return ()
    if isinstance(value, CedarEntity):
        return (value,)
    if isinstance(value, Iterable) and not isinstance(value, str | Mapping):
        entities = []
        for item in value:
            if not isinstance(item, CedarEntity):
                raise TypeError(
                    f"entities must be CedarEntity instances, got {type(item).__name__!r}"
                )
            entities.append(item)
        return tuple(entities)
    raise TypeError(f"entities must be CedarEntity instances, got {type(value).__name__!r}")
