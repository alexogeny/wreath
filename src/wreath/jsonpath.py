"""A bounded RFC 9535 JSONPath compiler and evaluator."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Final
from unicodedata import category as _unicode_category

from ._json import jsonpath_find as _native_find
from ._json import loads as _json_loads

__all__ = ["JSONPath", "JSONPathError", "JSONPathMatch", "compile_jsonpath", "jsonpath"]

_IJSON_MAX: Final = (1 << 53) - 1
_NOTHING = object()
_IREGEXP_PROPERTIES: Final = frozenset(
    {
        "L", "Ll", "Lm", "Lo", "Lt", "Lu",
        "M", "Mc", "Me", "Mn",
        "N", "Nd", "Nl", "No",
        "P", "Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps",
        "Z", "Zl", "Zp", "Zs",
        "S", "Sc", "Sk", "Sm", "So",
        "C", "Cc", "Cf", "Cn", "Co",
    }
)
_IREGEXP_ESCAPES: Final = frozenset("()*+-.?[\\]^nrt{}")


class JSONPathError(ValueError):
    """A JSONPath is invalid or exceeds its configured evaluation bound."""


class _IRegexpError(ValueError):
    pass


class _IRegexp:
    __slots__ = ("index", "length", "source", "value")

    def __init__(self, source: str, value: str) -> None:
        self.source = source
        self.value = value
        self.length = len(source)
        self.index = 0

    def parse(self) -> str:
        translated = self.regexp()
        if self.index != self.length:
            raise _IRegexpError
        return translated

    def regexp(self) -> str:
        branches = [self.branch()]
        while self.take("|"):
            branches.append(self.branch())
        return "|".join(branches)

    def branch(self) -> str:
        pieces = []
        while self.index < self.length and self.source[self.index] not in "|)":
            pieces.append(self.piece())
        return "".join(pieces)

    def piece(self) -> str:
        atom = self.atom()
        if self.index >= self.length:
            return atom
        char = self.source[self.index]
        if char in "*+?":
            self.index += 1
            return atom + char
        if char != "{":
            return atom
        start = self.index
        self.index += 1
        digits = self.digits()
        if not digits:
            raise _IRegexpError
        if self.take(","):
            self.digits()
        if not self.take("}"):
            raise _IRegexpError
        return atom + self.source[start : self.index]

    def atom(self) -> str:
        if self.take("("):
            expression = self.regexp()
            if not self.take(")"):
                raise _IRegexpError
            return f"(?:{expression})"
        if self.take("."):
            return "[^\\n\\r]"
        if self.take("["):
            return self.character_class()
        if self.take("\\"):
            return self.escape(in_class=False)
        if self.index >= self.length:
            raise _IRegexpError
        char = self.source[self.index]
        codepoint = ord(char)
        allowed = (
            codepoint <= 0x27
            or char in ",-"
            or "/" <= char <= ">"
            or "@" <= char <= "Z"
            or "^" <= char <= "z"
            or 0x7E <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0x10FFFF
        )
        if not allowed:
            raise _IRegexpError
        self.index += 1
        return char if char in "^$" else re.escape(char)

    def character_class(self) -> str:
        translated = ["["]
        if self.take("^"):
            translated.append("^")
        members = 0
        if self.take("-"):
            translated.append("\\-")
            members += 1
        while self.index < self.length and self.source[self.index] != "]":
            first, property_escape = self.class_atom()
            members += 1
            if not property_escape and self.take("-"):
                if self.index < self.length and self.source[self.index] != "]":
                    second, second_property = self.class_atom()
                    if second_property:
                        raise _IRegexpError
                    translated.extend((first, "-", second))
                    continue
                translated.extend((first, "\\-"))
                continue
            translated.append(first)
        if not members or not self.take("]"):
            raise _IRegexpError
        translated.append("]")
        return "".join(translated)

    def class_atom(self) -> tuple[str, bool]:
        if self.take("\\"):
            start = self.index
            escaped = self.escape(in_class=True)
            return escaped, self.source[start : start + 1] in {"p", "P"}
        if self.index >= self.length:
            raise _IRegexpError
        char = self.source[self.index]
        codepoint = ord(char)
        if char in "-[]\\" or 0xD800 <= codepoint <= 0xDFFF:
            raise _IRegexpError
        self.index += 1
        return re.escape(char), False

    def escape(self, *, in_class: bool) -> str:
        if self.index >= self.length:
            raise _IRegexpError
        escape = self.source[self.index]
        self.index += 1
        if escape in {"p", "P"}:
            if not self.take("{"):
                raise _IRegexpError
            start = self.index
            while self.index < self.length and self.source[self.index] != "}":
                self.index += 1
            name = self.source[start : self.index]
            if name not in _IREGEXP_PROPERTIES or not self.take("}"):
                raise _IRegexpError
            chars = self.property_chars(name, escape == "P")
            return chars if in_class else f"[{chars}]"
        if escape not in _IREGEXP_ESCAPES:
            raise _IRegexpError
        return f"\\{escape}"

    def property_chars(self, name: str, complement: bool) -> str:
        matched = set()
        for char in self.value:
            scalar = ord(char)
            if 0xD800 <= scalar <= 0xDFFF:
                raise _IRegexpError
            category = _unicode_category(char)
            belongs = category == name if len(name) == 2 else category.startswith(name)
            if belongs != complement:
                matched.add(char)
        if not matched:
            for scalar in range(128):
                candidate = chr(scalar)
                if candidate not in self.value:
                    matched.add(candidate)
                    break
        return "".join(re.escape(char) for char in sorted(matched))

    def digits(self) -> str:
        start = self.index
        while self.index < self.length and "0" <= self.source[self.index] <= "9":
            self.index += 1
        return self.source[start : self.index]

    def take(self, text: str) -> bool:
        if self.source.startswith(text, self.index):
            self.index += len(text)
            return True
        return False


def _iregexp_fullmatch(pattern: str, value: str) -> object | None:
    try:
        return re.fullmatch(_IRegexp(pattern, value).parse(), value)
    except (_IRegexpError, re.PatternError, OverflowError):
        return None


def _iregexp_search(pattern: str, value: str) -> object | None:
    try:
        return re.search(_IRegexp(pattern, value).parse(), value)
    except (_IRegexpError, re.PatternError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class JSONPathMatch:
    """One selected JSON value and its normalized absolute path."""

    value: Any
    path: str


@dataclass(frozen=True, slots=True)
class _Node:
    value: Any
    tokens: tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class _Name:
    value: str


@dataclass(frozen=True, slots=True)
class _Index:
    value: int


@dataclass(frozen=True, slots=True)
class _Slice:
    start: int | None
    stop: int | None
    step: int | None


@dataclass(frozen=True, slots=True)
class _Wildcard:
    pass


@dataclass(frozen=True, slots=True)
class _Filter:
    expression: Any


@dataclass(frozen=True, slots=True)
class _Segment:
    descendant: bool
    selectors: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _Literal:
    value: Any


@dataclass(frozen=True, slots=True)
class _Query:
    root: bool
    segments: tuple[_Segment, ...]
    singular: bool


@dataclass(frozen=True, slots=True)
class _Function:
    name: str
    arguments: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _Compare:
    left: Any
    operator: str
    right: Any


@dataclass(frozen=True, slots=True)
class _Not:
    expression: Any


@dataclass(frozen=True, slots=True)
class _Logical:
    left: Any
    operator: str
    right: Any


class _Parser:
    __slots__ = ("index", "length", "source")

    def __init__(self, source: str) -> None:
        if not isinstance(source, str):
            raise TypeError("JSONPath source must be a string")
        if not source or len(source) > 16 * 1024:
            raise JSONPathError("JSONPath must contain between 1 and 16384 characters")
        self.source = source
        self.length = len(source)
        self.index = 0

    def error(self, message: str) -> JSONPathError:
        return JSONPathError(f"{message} at character {self.index}")

    def peek(self, text: str = "") -> bool:
        return self.source.startswith(text, self.index) if text else self.index < self.length

    def take(self, text: str) -> bool:
        if self.peek(text):
            self.index += len(text)
            return True
        return False

    def whitespace(self) -> None:
        while self.index < self.length and self.source[self.index] in " \t\n\r":
            self.index += 1

    def parse(self) -> tuple[_Segment, ...]:
        if not self.take("$"):
            raise self.error("JSONPath must begin with '$'")
        segments = self.segments(in_filter=False)
        if self.index != self.length:
            raise self.error("unexpected JSONPath input")
        return segments

    def segments(self, *, in_filter: bool) -> tuple[_Segment, ...]:
        segments: list[_Segment] = []
        while self.index < self.length:
            before_space = self.index
            self.whitespace()
            if in_filter and (
                self.peek("&&")
                or self.peek("||")
                or self.peek("==")
                or self.peek("!=")
                or self.peek("<=")
                or self.peek(">=")
                or self.source[self.index] in "<>)!,"
            ):
                break
            if self.take(".."):
                segments.append(_Segment(True, self.segment_selectors()))
            elif self.take("."):
                segments.append(_Segment(False, self.dot_selectors()))
            elif self.peek("["):
                segments.append(_Segment(False, self.bracket_selectors()))
            else:
                self.index = before_space
                break
        return tuple(segments)

    def segment_selectors(self) -> tuple[Any, ...]:
        if self.peek("["):
            return self.bracket_selectors()
        return self.dot_selectors()

    def dot_selectors(self) -> tuple[Any, ...]:
        if self.take("*"):
            return (_Wildcard(),)
        start = self.index
        if self.index >= self.length or not self.name_first(self.source[self.index]):
            raise self.error("member-name shorthand needs a name or '*'")
        self.index += 1
        while self.index < self.length and self.name_char(self.source[self.index]):
            self.index += 1
        return (_Name(self.source[start : self.index]),)

    @staticmethod
    def name_first(char: str) -> bool:
        codepoint = ord(char)
        return (
            char == "_"
            or "A" <= char <= "Z"
            or "a" <= char <= "z"
            or 0x80 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0x10FFFF
        )

    @classmethod
    def name_char(cls, char: str) -> bool:
        return cls.name_first(char) or "0" <= char <= "9"

    def bracket_selectors(self) -> tuple[Any, ...]:
        if not self.take("["):
            raise self.error("expected '['")
        selectors: list[Any] = []
        while True:
            self.whitespace()
            selectors.append(self.selector())
            self.whitespace()
            if self.take("]"):
                break
            if not self.take(","):
                raise self.error("selector list needs ',' or ']'")
        return tuple(selectors)

    def selector(self) -> Any:
        if self.take("*"):
            return _Wildcard()
        if self.take("?"):
            return _Filter(self.logical_or())
        if self.peek("'") or self.peek('"'):
            return _Name(self.string())
        return self.index_or_slice()

    def string(self) -> str:
        quote = self.source[self.index]
        self.index += 1
        encoded = ['"']
        while self.index < self.length:
            char = self.source[self.index]
            self.index += 1
            if char == quote:
                break
            codepoint = ord(char)
            if codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
                raise self.error("JSONPath string literal contains a non-scalar character")
            if char != "\\":
                encoded.append('\\"' if char == '"' else char)
                continue
            if self.index >= self.length:
                break
            escape = self.source[self.index]
            self.index += 1
            if escape == quote:
                encoded.append("'" if quote == "'" else '\\"')
            elif escape in "bfnrt/\\":
                encoded.extend(("\\", escape))
            elif escape == "u":
                encoded.append(self.unicode_escape())
            else:
                raise self.error("invalid JSONPath string escape")
        else:
            raise self.error("unterminated JSONPath string literal")
        encoded.append('"')
        try:
            value = _json_loads("".join(encoded))
        except ValueError:
            raise self.error("invalid JSONPath string literal") from None
        if not isinstance(value, str):
            raise self.error("JSONPath string literal did not decode to text")
        return value

    def unicode_escape(self) -> str:
        start = self.index
        end = start + 4
        digits = self.source[start:end]
        if len(digits) != 4 or any(char not in "0123456789abcdefABCDEF" for char in digits):
            raise self.error("JSONPath Unicode escape needs four hexadecimal digits")
        self.index = end
        codepoint = int(digits, 16)
        encoded = f"\\u{digits}"
        if 0xD800 <= codepoint <= 0xDBFF:
            if not self.take("\\u"):
                raise self.error("high surrogate must be followed by a low surrogate")
            low_start = self.index
            low_end = low_start + 4
            low_digits = self.source[low_start:low_end]
            if len(low_digits) != 4 or any(
                char not in "0123456789abcdefABCDEF" for char in low_digits
            ):
                raise self.error("low surrogate needs four hexadecimal digits")
            low = int(low_digits, 16)
            if not 0xDC00 <= low <= 0xDFFF:
                raise self.error("high surrogate must be followed by a low surrogate")
            self.index = low_end
            return f"{encoded}\\u{low_digits}"
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise self.error("low surrogate must follow a high surrogate")
        return encoded

    def integer(self, *, optional: bool = False) -> int | None:
        start = self.index
        self.take("-")
        digits = self.index
        while self.index < self.length and self.source[self.index].isdigit():
            self.index += 1
        if digits == self.index:
            self.index = start
            if optional:
                return None
            raise self.error("expected an integer selector")
        raw = self.source[start : self.index]
        if raw in {"-0"} or (raw.startswith("0") and len(raw) > 1) or raw.startswith("-0"):
            raise self.error("JSONPath integer must use canonical decimal syntax")
        value = int(raw)
        if abs(value) > _IJSON_MAX:
            raise self.error("JSONPath integer is outside the I-JSON exact range")
        return value

    def index_or_slice(self) -> Any:
        start = self.integer(optional=True)
        self.whitespace()
        if not self.take(":"):
            if start is None:
                raise self.error("expected a name, wildcard, index, slice, or filter selector")
            return _Index(start)
        self.whitespace()
        stop = self.integer(optional=True)
        self.whitespace()
        step = None
        if self.take(":"):
            self.whitespace()
            step = self.integer(optional=True)
        return _Slice(start, stop, step)

    def logical_or(self) -> Any:
        expression = self.logical_and()
        while True:
            self.whitespace()
            if not self.take("||"):
                return expression
            self.whitespace()
            expression = _Logical(expression, "||", self.logical_and())

    def logical_and(self) -> Any:
        expression = self.logical_unary()
        while True:
            self.whitespace()
            if not self.take("&&"):
                return expression
            self.whitespace()
            expression = _Logical(expression, "&&", self.logical_unary())

    def logical_unary(self) -> Any:
        self.whitespace()
        negated = self.take("!")
        if negated:
            self.whitespace()
        if self.take("("):
            expression = self.logical_or()
            self.whitespace()
            if not self.take(")"):
                raise self.error("filter expression needs ')'")
            return _Not(expression) if negated else expression
        left = self.operand()
        self.whitespace()
        operator = next(
            (candidate for candidate in ("==", "!=", "<=", ">=", "<", ">") if self.take(candidate)),
            None,
        )
        if operator is None:
            if isinstance(left, _Query) or (
                isinstance(left, _Function) and left.name in {"match", "search"}
            ):
                return _Not(left) if negated else left
            raise self.error("literal filter operand needs a comparison")
        if negated:
            raise self.error("a negated comparison must be parenthesized")
        self.whitespace()
        right = self.operand()
        if not self.value_operand(left) or not self.value_operand(right):
            raise self.error("comparison operands must be values or singular queries")
        return _Compare(left, operator, right)

    @staticmethod
    def value_operand(expression: Any) -> bool:
        return (
            isinstance(expression, _Literal)
            or isinstance(expression, _Query)
            and expression.singular
            or isinstance(expression, _Function)
            and expression.name in {"length", "count", "value"}
        )

    def operand(self) -> Any:
        self.whitespace()
        if self.peek("$") or self.peek("@"):
            return self.query()
        if self.peek("'") or self.peek('"'):
            return _Literal(self.string())
        for word, value in (("true", True), ("false", False), ("null", None)):
            if self.take(word):
                return _Literal(value)
        if self.index < self.length and (
            self.source[self.index].isdigit() or self.source[self.index] == "-"
        ):
            return _Literal(self.number())
        name_start = self.index
        while self.index < self.length and (
            "a" <= self.source[self.index] <= "z"
            or "0" <= self.source[self.index] <= "9"
            or self.source[self.index] == "_"
        ):
            self.index += 1
        if self.index > name_start and self.take("("):
            return self.function(self.source[name_start : self.index - 1])
        self.index = name_start
        raise self.error("expected a filter operand")

    def number(self) -> int | float:
        match = re.match(
            r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",
            self.source[self.index :],
        )
        if match is None:
            raise self.error("invalid JSON number")
        self.index += len(match.group(0))
        value = _json_loads(match.group(0))
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise self.error("filter number is outside the interoperable JSON range")
        return value

    def query(self) -> _Query:
        root = self.take("$")
        if not root and not self.take("@"):
            raise self.error("query must start with '$' or '@'")
        segments = self.segments(in_filter=True)
        singular = all(
            not segment.descendant
            and len(segment.selectors) == 1
            and isinstance(segment.selectors[0], (_Name, _Index))
            for segment in segments
        )
        return _Query(root, segments, singular)

    def function(self, name: str) -> _Function:
        signatures = {
            "length": 1,
            "count": 1,
            "value": 1,
            "match": 2,
            "search": 2,
        }
        arity = signatures.get(name)
        if arity is None:
            raise self.error(f"unknown JSONPath function {name!r}")
        arguments: list[Any] = []
        self.whitespace()
        if not self.take(")"):
            while True:
                arguments.append(self.operand())
                self.whitespace()
                if self.take(")"):
                    break
                if not self.take(","):
                    raise self.error("function arguments need ',' or ')'")
                self.whitespace()
        if len(arguments) != arity:
            raise self.error(f"JSONPath function {name} needs {arity} argument(s)")
        if name == "length" and isinstance(arguments[0], _Query) and not arguments[0].singular:
            raise self.error("length() needs a value or singular query")
        if name in {"length", "match", "search"} and any(
            not self.value_operand(argument) for argument in arguments
        ):
            raise self.error(f"{name}() needs value or singular-query arguments")
        if name in {"count", "value"} and not isinstance(arguments[0], _Query):
            raise self.error(f"{name}() needs a query argument")
        return _Function(name, tuple(arguments))


def _native_expression(expression: Any) -> tuple[Any, ...]:
    if isinstance(expression, _Literal):
        return (0, expression.value)
    if isinstance(expression, _Query):
        return (1, expression.root, _native_segments(expression.segments), expression.singular)
    if isinstance(expression, _Function):
        functions = {"length": 0, "count": 1, "value": 2, "match": 3, "search": 4}
        return (
            2,
            functions[expression.name],
            tuple(_native_expression(argument) for argument in expression.arguments),
        )
    if isinstance(expression, _Compare):
        comparisons = {"==": 0, "!=": 1, "<=": 2, ">=": 3, "<": 4, ">": 5}
        return (
            3,
            comparisons[expression.operator],
            _native_expression(expression.left),
            _native_expression(expression.right),
        )
    if isinstance(expression, _Not):
        return (4, _native_expression(expression.expression))
    return (
        5,
        expression.operator == "&&",
        _native_expression(expression.left),
        _native_expression(expression.right),
    )


def _native_selector(selector: Any) -> tuple[Any, ...]:
    if isinstance(selector, _Name):
        return (0, selector.value)
    if isinstance(selector, _Index):
        return (1, selector.value)
    if isinstance(selector, _Slice):
        return (2, selector.start, selector.stop, selector.step)
    if isinstance(selector, _Wildcard):
        return (3,)
    return (4, _native_expression(selector.expression))


def _native_segments(segments: tuple[_Segment, ...]) -> tuple[Any, ...]:
    return tuple(
        (segment.descendant, tuple(_native_selector(selector) for selector in segment.selectors))
        for segment in segments
    )


def _children(node: _Node) -> list[_Node]:
    value = node.value
    if isinstance(value, dict):
        return [_Node(child, (*node.tokens, name)) for name, child in value.items()]
    if isinstance(value, list):
        return [_Node(child, (*node.tokens, index)) for index, child in enumerate(value)]
    return []


def _descendants(node: _Node, visit: Any) -> list[_Node]:
    found = []
    pending = [node]
    while pending:
        current = pending.pop()
        found.append(current)
        children = _children(current)
        for _child in children:
            visit()
        pending.extend(reversed(children))
    return found


def _select(selector: Any, node: _Node, root: _Node, visit: Any) -> list[_Node]:
    value = node.value
    if isinstance(selector, _Name):
        if isinstance(value, dict) and selector.value in value:
            visit()
            return [_Node(value[selector.value], (*node.tokens, selector.value))]
        return []
    if isinstance(selector, _Index):
        if not isinstance(value, list):
            return []
        index = selector.value if selector.value >= 0 else len(value) + selector.value
        if 0 <= index < len(value):
            visit()
            return [_Node(value[index], (*node.tokens, index))]
        return []
    if isinstance(selector, _Slice):
        if not isinstance(value, list) or selector.step == 0:
            return []
        indices = range(*slice(selector.start, selector.stop, selector.step).indices(len(value)))
        selected = []
        for index in indices:
            visit()
            selected.append(_Node(value[index], (*node.tokens, index)))
        return selected
    if isinstance(selector, _Wildcard):
        selected = _children(node)
        for _child in selected:
            visit()
        return selected
    selected = []
    for child in _children(node):
        visit()
        if _truth(_evaluate(selector.expression, root, child, visit)):
            selected.append(child)
    return selected


def _run(
    segments: tuple[_Segment, ...], nodes: list[_Node], root: _Node, visit: Any
) -> list[_Node]:
    current = nodes
    for segment in segments:
        sources: list[_Node] = []
        if segment.descendant:
            for node in current:
                sources.extend(_descendants(node, visit))
        else:
            sources = current
        selected: list[_Node] = []
        for source in sources:
            for selector in segment.selectors:
                selected.extend(_select(selector, source, root, visit))
        current = selected
    return current


def _operand(expression: Any, root: _Node, current: _Node, visit: Any) -> Any:
    if isinstance(expression, _Literal):
        return expression.value
    if isinstance(expression, _Query):
        nodes = _run(expression.segments, [root if expression.root else current], root, visit)
        if expression.singular:
            return nodes[0].value if len(nodes) == 1 else _NOTHING
        return nodes
    if isinstance(expression, _Function):
        if expression.name in {"count", "value"}:
            query = expression.arguments[0]
            nodes = _run(
                query.segments,
                [root if query.root else current],
                root,
                visit,
            )
            if expression.name == "count":
                return len(nodes)
            return nodes[0].value if len(nodes) == 1 else _NOTHING
        arguments = tuple(
            _operand(argument, root, current, visit) for argument in expression.arguments
        )
        if expression.name == "length":
            value = arguments[0]
            return len(value) if isinstance(value, (str, list, dict)) else _NOTHING
        if not all(isinstance(value, str) for value in arguments):
            return False
        matched = (
            _iregexp_fullmatch(arguments[1], arguments[0])
            if expression.name == "match"
            else _iregexp_search(arguments[1], arguments[0])
        )
        return matched is not None
    return _evaluate(expression, root, current, visit)


def _equal(left: Any, right: Any) -> bool:
    if left is _NOTHING or right is _NOTHING:
        return left is right
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        return isinstance(right, (int, float)) and not isinstance(right, bool) and left == right
    return type(left) is type(right) and left == right


def _evaluate(expression: Any, root: _Node, current: _Node, visit: Any) -> Any:
    if isinstance(expression, _Logical):
        left = _truth(_evaluate(expression.left, root, current, visit))
        if expression.operator == "&&":
            return left and _truth(_evaluate(expression.right, root, current, visit))
        return left or _truth(_evaluate(expression.right, root, current, visit))
    if isinstance(expression, _Not):
        return not _truth(_evaluate(expression.expression, root, current, visit))
    if isinstance(expression, _Compare):
        left = _operand(expression.left, root, current, visit)
        right = _operand(expression.right, root, current, visit)
        if expression.operator == "==":
            return _equal(left, right)
        if expression.operator == "!=":
            return not _equal(left, right)
        orderable = (
            isinstance(left, str)
            and isinstance(right, str)
            or isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        )
        less = orderable and left < right
        reverse_less = orderable and right < left
        equal = _equal(left, right)
        if expression.operator == "<":
            return less
        if expression.operator == "<=":
            return less or equal
        if expression.operator == ">":
            return reverse_less
        return reverse_less or equal
    if isinstance(expression, _Query):
        return bool(
            _run(
                expression.segments,
                [root if expression.root else current],
                root,
                visit,
            )
        )
    value = _operand(expression, root, current, visit)
    return bool(value) if not isinstance(value, list) else bool(value)


def _truth(value: Any) -> bool:
    if value is _NOTHING:
        return False
    if isinstance(value, list):
        return bool(value)
    return bool(value)


def _normalized(tokens: tuple[str | int, ...]) -> str:
    path = ["$"]
    short_escapes = {
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
        "'": "\\'",
        "\\": "\\\\",
    }
    for token in tokens:
        if isinstance(token, int):
            path.append(f"[{token}]")
            continue
        escaped = "".join(
            short_escapes.get(char, f"\\u{ord(char):04x}" if ord(char) < 0x20 else char)
            for char in token
        )
        path.append(f"['{escaped}']")
    return "".join(path)


@dataclass(frozen=True, slots=True)
class JSONPath:
    """A compiled JSONPath expression reusable across JSON values."""

    source: str
    _segments: tuple[_Segment, ...]
    _program: tuple[Any, ...]
    max_visits: int = 1_000_000

    def find(self, value: Any) -> list[JSONPathMatch]:
        nodes = _native_find(
            self._program,
            value,
            self.max_visits,
            JSONPathError,
            _iregexp_fullmatch,
            _iregexp_search,
            re.PatternError,
        )
        return [JSONPathMatch(node, _normalized(tokens)) for node, tokens in nodes]

    def _find_reference(self, value: Any) -> list[JSONPathMatch]:
        remaining = self.max_visits

        def visit() -> None:
            nonlocal remaining
            remaining -= 1
            if remaining < 0:
                raise JSONPathError(
                    f"JSONPath evaluation exceeded its {self.max_visits} node visit limit"
                )

        root = _Node(value, ())
        nodes = _run(self._segments, [root], root, visit)
        return [JSONPathMatch(node.value, _normalized(node.tokens)) for node in nodes]


def compile_jsonpath(source: str, *, max_visits: int = 1_000_000) -> JSONPath:
    """Compile and validate an RFC 9535 JSONPath expression."""
    if max_visits <= 0:
        raise ValueError("JSONPath max_visits must be positive")
    segments = _Parser(source).parse()
    return JSONPath(source, segments, _native_segments(segments), max_visits)


def jsonpath(source: str | JSONPath, value: Any) -> list[Any]:
    """Return the selected values for a source string or compiled JSONPath."""
    compiled = source if isinstance(source, JSONPath) else compile_jsonpath(source)
    return [match.value for match in compiled.find(value)]
