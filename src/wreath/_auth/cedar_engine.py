"""The built-in Cedar policy engine: parse and compile once, evaluate native.

This module implements the Cedar policy language core with no dependencies:
the policy grammar (`permit`/`forbid`, scope constraints, `when`/
`unless` conditions), the strict expression language, the entity hierarchy,
and the authorization algorithm (forbid overrides permit; default deny; an
erroring policy is skipped and reported, never silently satisfied).

The split follows Wreath's usual shape. Parsing and compilation happen here,
in Python, exactly once — at application startup, where a syntax error is an
application bug and not a request-time surprise. The compiled program is a
flat tuple tape; the per-request evaluator that walks it is native C
(`wreath._native._core.cedar_is_authorized`) with a pure-Python twin of
identical observable behavior in `wreath._pure.cedar`.

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

Compiled value model (shared by both evaluators): `bool`, i64 `int`,
`str`, an entity uid as a `(type, id)` string 2-tuple, a set as a
duplicate-free `list`, and a record as a `dict`. Booleans are never
integers — every check tests `bool` first, because Python's bool subclasses
int and Cedar's type system does not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .._native import _core
from .._pure import cedar as _pure_cedar
from .models import AuthorizationDecision

__all__ = [
    "CedarEntity",
    "CedarParseError",
    "CedarPolicies",
    "EntityUid",
]

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

# Expression opcodes; wreath/_native/cedar.c and wreath/_pure/cedar.py agree.
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


def _cedar_eq(a: Any, b: Any) -> bool:
    """Structural equality over the compiled value model; never an error."""
    if type(a) is bool or type(b) is bool:
        return type(a) is bool and type(b) is bool and a is b
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, tuple) and isinstance(b, tuple):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return all(any(_cedar_eq(x, y) for y in b) for x in a) and all(
            any(_cedar_eq(y, x) for x in a) for y in b
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_cedar_eq(value, b[key]) for key, value in a.items())
    return False


def _dedupe_key(value: Any) -> tuple[str, object] | None:
    """A hashable identity for a converted value, or None if it needs `_cedar_eq`.

    The tag keeps kinds apart because `_cedar_eq` does: `True` and `1`
    are *not* equal in Cedar's model, but in Python they compare equal and hash
    alike, so an untagged set would silently merge them. Values of different
    kinds are never `_cedar_eq`, so partitioning by kind loses nothing.
    """
    if type(value) is bool:
        return ("b", value)
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, tuple):
        # An entity uid, so (str, str) and hashable; guarded anyway rather than
        # assuming, since falling back is merely slower and never wrong.
        try:
            hash(value)
        except TypeError:
            return None
        return ("t", value)
    return None  # records and nested sets: structural comparison only


def _to_cedar_value(value: object, *, where: str) -> Any:
    """Convert a Python value into the compiled value model, loudly or not at all."""
    if type(value) is bool or isinstance(value, str):
        return value
    if isinstance(value, int):
        if not _I64_MIN <= value <= _I64_MAX:
            raise TypeError(f"{where}: integer {value} does not fit in Cedar's i64")
        return value
    if isinstance(value, EntityUid):
        return (value.type, value.id)
    if isinstance(value, Mapping):
        converted: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"{where}: record keys must be strings, got {type(key).__name__!r}"
                )
            converted[key] = _to_cedar_value(item, where=where)
        return converted
    if isinstance(value, list | tuple | set | frozenset):
        # A Cedar set is unordered with structural equality, so duplicates have
        # to go. Comparing every candidate against every kept one is O(N**2),
        # and this runs on every `is_authorized` call -- once for the context
        # and once per entity attribute -- so a policy carrying a few hundred
        # group ids paid it per authorization. Measured before this change: 25
        # elements 64us, 400 elements 14.4ms, a clean 4x per doubling.
        #
        # Scalars carry their own identity, so they dedupe through a set. Only
        # records and nested sets, which are unhashable and need structural
        # comparison, keep the pairwise scan -- and they compare against just
        # the other unhashables rather than the whole result.
        deduplicated: list[object] = []
        seen: set[tuple[str, object]] = set()
        structural: list[object] = []
        for item in value:
            candidate = _to_cedar_value(item, where=where)
            key = _dedupe_key(candidate)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            else:
                if any(_cedar_eq(candidate, existing) for existing in structural):
                    continue
                structural.append(candidate)
            deduplicated.append(candidate)
        return deduplicated
    raise TypeError(
        f"{where}: {type(value).__name__!r} has no Cedar equivalent; "
        "use bool, int, str, EntityUid, a mapping, or a sequence"
    )


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


def _build_store(
    entities: Iterable[CedarEntity],
) -> dict[tuple[str, str], tuple[dict[str, object], frozenset[tuple[str, str]]]]:
    """Compile entities into the evaluator's store: uid -> (attrs, ancestors).

    Ancestors are the *transitive* closure, precomputed here so neither
    evaluator ever walks the hierarchy at request time — `in` is one set
    membership test. Cycles are tolerated (an entity is never its own
    ancestor unless a cycle makes it one, matching a fixed-point closure).
    """
    attrs: dict[tuple[str, str], dict[str, object]] = {}
    parents: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    for entity in entities:
        if not isinstance(entity, CedarEntity):
            raise TypeError(
                f"entities must be CedarEntity instances, got {type(entity).__name__!r}"
            )
        uid = (entity.uid.type, entity.uid.id)
        where = f"entity {entity.uid}"
        converted = _to_cedar_value(dict(entity.attrs), where=where)
        attrs[uid] = converted
        parent_uids = []
        for parent in entity.parents:
            if not isinstance(parent, EntityUid):
                raise TypeError(f"{where}: parents must be EntityUid instances")
            parent_uids.append((parent.type, parent.id))
        parents[uid] = tuple(parent_uids)
    store: dict[tuple[str, str], tuple[dict[str, object], frozenset[tuple[str, str]]]] = {}
    for uid, attributes in attrs.items():
        seen: set[tuple[str, str]] = set()
        frontier = list(parents.get(uid, ()))
        while frontier:
            parent = frontier.pop()
            if parent in seen:
                continue
            seen.add(parent)
            frontier.extend(parents.get(parent, ()))
        store[uid] = (attributes, frozenset(seen))
    return store


# -- the engine ---------------------------------------------------------------


class CedarPolicies:
    """A parsed Cedar policy set that acts as the authorization engine.

    Parsing happens once, at construction — a syntax error is an application
    bug and surfaces at startup, never during a request. `entities` given
    here are the static hierarchy (roles, groups, resource ownership); the
    per-request entities handed to `is_authorized` are merged over them.
    """

    __slots__ = ("_entities", "_policies", "_source", "_store")

    def __init__(self, source: str, *, entities: Iterable[CedarEntity] = ()) -> None:
        if not isinstance(source, str) or not source.strip():
            raise CedarParseError("policy source must be non-empty Cedar text")
        self._source = source
        self._policies = _Parser(_tokenize(source)).policies()
        self._entities = tuple(entities)
        self._store = _build_store(self._entities)

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

    def is_authorized(
        self,
        *,
        principal: object,
        action: object,
        resource: object,
        context: Mapping[str, object] | None = None,
        entities: object = None,
    ) -> AuthorizationDecision:
        request_entities = _as_entities(entities)
        if request_entities:
            store = _build_store(self._entities + request_entities)
        else:
            store = self._store
        evaluate = _pure_cedar.cedar_is_authorized
        native = getattr(_core, "cedar_is_authorized", None) if _core is not None else None
        if native is not None:
            evaluate = native
        allowed, reason, diagnostics = evaluate(
            self._policies,
            _as_uid_tuple(principal, "principal"),
            _as_uid_tuple(action, "action"),
            _as_uid_tuple(resource, "resource"),
            _to_cedar_value(dict(context or {}), where="context"),
            store,
        )
        return AuthorizationDecision(allowed, reason, diagnostics)


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
