from __future__ import annotations

import itertools
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

GenerateHook = Callable[[random.Random, int], bytes]
MutateHook = Callable[[bytes, random.Random, int], bytes]
CrossoverHook = Callable[[bytes, bytes, random.Random, int], bytes]
ShrinkHook = Callable[[bytes], Iterable[bytes]]
DictionaryHook = Callable[[bytes], Iterable[bytes]]

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_HTTP = re.compile(
    rb"(?P<method>GET|HEAD|POST|PUT) (?P<target>/[a-z0-9/_-]*) HTTP/1\.(?P<minor>[01])\r\n"
    rb"Host: (?P<host>[a-z0-9.-]+)\r\n"
    rb"(?:Content-Length: (?P<length>[0-9]+)\r\n)?\r\n(?P<body>.*)\Z",
    re.DOTALL,
)
_XML = re.compile(
    rb"<(?P<tag>[a-z]+)(?: id=\"(?P<identifier>[a-z0-9]+)\")?>"
    rb"(?P<text>[a-z0-9 ]*)(?:<(?P<child>[a-z]+)/>)?</(?P=tag)>\Z"
)
_XML_EMPTY = re.compile(rb"<(?P<tag>[a-z]+)/>\Z")


@dataclass(frozen=True, slots=True)
class StructuredStrategy:
    name: str
    version: int
    seeds: tuple[bytes, ...] = ()
    dictionary: tuple[bytes, ...] = ()
    generate: GenerateHook | None = None
    mutate: MutateHook | None = None
    crossover: CrossoverHook | None = None
    shrink: ShrinkHook | None = None
    dictionary_hook: DictionaryHook | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError(
                "structured strategy name must use lowercase letters, digits, '.', '_', or '-'"
            )
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("structured strategy version must be a positive integer")
        if not isinstance(self.seeds, tuple) or any(
            not isinstance(value, bytes) for value in self.seeds
        ):
            raise TypeError("structured strategy seeds must be a tuple of bytes")
        if not isinstance(self.dictionary, tuple) or any(
            not isinstance(value, bytes) or not value for value in self.dictionary
        ):
            raise TypeError("structured strategy dictionary must be a tuple of non-empty bytes")
        for field_name in (
            "generate",
            "mutate",
            "crossover",
            "shrink",
            "dictionary_hook",
        ):
            hook = getattr(self, field_name)
            if hook is not None and not callable(hook):
                raise TypeError(f"structured strategy {field_name} must be callable or None")

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def generate_case(self, rng: random.Random, max_size: int) -> bytes | None:
        _positive("max_size", max_size)
        if self.generate is None:
            return None
        return _bounded("generate", self.generate(rng, max_size), max_size)

    def mutate_case(self, data: bytes, rng: random.Random, max_size: int) -> bytes | None:
        _input(data)
        _positive("max_size", max_size)
        if self.mutate is None:
            return None
        return _bounded("mutate", self.mutate(data, rng, max_size), max_size)

    def crossover_case(
        self,
        left: bytes,
        right: bytes,
        rng: random.Random,
        max_size: int,
    ) -> bytes | None:
        _input(left)
        _input(right)
        _positive("max_size", max_size)
        if self.crossover is None:
            return None
        return _bounded("crossover", self.crossover(left, right, rng, max_size), max_size)

    def shrink_cases(
        self,
        data: bytes,
        *,
        max_candidates: int,
        max_size: int,
    ) -> tuple[bytes, ...]:
        _input(data)
        _positive("max_candidates", max_candidates)
        _positive("max_size", max_size)
        if self.shrink is None:
            return ()
        candidates: list[bytes] = []
        seen: set[bytes] = set()
        limit = max_candidates * 8
        for candidate in itertools.islice(self.shrink(data), limit):
            _input(candidate)
            if candidate in seen or len(candidate) > max_size or len(candidate) >= len(data):
                continue
            seen.add(candidate)
            candidates.append(candidate)
            if len(candidates) == max_candidates:
                break
        return tuple(candidates)

    def dictionary_tokens(
        self,
        data: bytes,
        *,
        max_tokens: int,
        max_token_size: int,
    ) -> tuple[bytes, ...]:
        _input(data)
        _positive("max_tokens", max_tokens)
        _positive("max_token_size", max_token_size)
        candidates: Iterable[bytes] = self.dictionary
        if self.dictionary_hook is not None:
            candidates = itertools.chain(candidates, self.dictionary_hook(data))
        tokens: list[bytes] = []
        seen: set[bytes] = set()
        for token in itertools.islice(candidates, max_tokens * 8):
            _input(token)
            if not token or len(token) > max_token_size or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
            if len(tokens) == max_tokens:
                break
        return tuple(tokens)


@dataclass(frozen=True, slots=True)
class _HTTPRequest:
    method: bytes
    target: bytes
    minor: bytes
    host: bytes
    body: bytes


@dataclass(frozen=True, slots=True)
class _XMLDocument:
    tag: bytes
    identifier: bytes | None
    text: bytes
    child: bytes | None
    empty: bool = False


def _positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _input(data: bytes) -> None:
    if not isinstance(data, bytes):
        raise TypeError("structured fuzz inputs must be bytes")


def _bounded(operation: str, data: bytes, max_size: int) -> bytes:
    _input(data)
    if len(data) > max_size:
        raise ValueError(
            f"structured {operation} output exceeds max_size; return at most {max_size} bytes"
        )
    return data


def _parse_http(data: bytes) -> _HTTPRequest | None:
    match = _HTTP.fullmatch(data)
    if match is None:
        return None
    body = match.group("body")
    length = match.group("length")
    if (length is None and body) or (length is not None and int(length) != len(body)):
        return None
    return _HTTPRequest(
        match.group("method"),
        match.group("target"),
        match.group("minor"),
        match.group("host"),
        body,
    )


def _render_http(request: _HTTPRequest) -> bytes:
    head = [
        request.method,
        b" ",
        request.target,
        b" HTTP/1.",
        request.minor,
        b"\r\nHost: ",
        request.host,
        b"\r\n",
    ]
    if request.body:
        head.extend((b"Content-Length: ", str(len(request.body)).encode(), b"\r\n"))
    head.extend((b"\r\n", request.body))
    return b"".join(head)


def _small_http() -> _HTTPRequest:
    return _HTTPRequest(b"GET", b"/", b"1", b"a", b"")


def _http_generate(rng: random.Random, max_size: int) -> bytes:
    method = rng.choice((b"GET", b"HEAD", b"POST", b"PUT"))
    segments = rng.randint(0, 3)
    target = b"/" + b"/".join(
        rng.choice((b"a", b"api", b"items", b"v1", b"0")) for _ in range(segments)
    )
    body = (
        bytes(rng.choice(b"abc012 ") for _ in range(rng.randint(0, 16)))
        if method in {b"POST", b"PUT"}
        else b""
    )
    request = _HTTPRequest(
        method,
        target,
        rng.choice((b"0", b"1")),
        rng.choice((b"a", b"example.test", b"localhost")),
        body,
    )
    rendered = _render_http(request)
    return rendered if len(rendered) <= max_size else _render_http(_small_http())


def _http_mutate(data: bytes, rng: random.Random, max_size: int) -> bytes:
    request = _parse_http(data)
    if request is None:
        return _http_generate(rng, max_size)
    operation = rng.randrange(5)
    if operation == 0:
        request = _HTTPRequest(
            rng.choice((b"GET", b"HEAD", b"POST", b"PUT")),
            request.target,
            request.minor,
            request.host,
            request.body,
        )
    elif operation == 1:
        request = _HTTPRequest(
            request.method,
            rng.choice((b"/", b"/api", b"/items/0")),
            request.minor,
            request.host,
            request.body,
        )
    elif operation == 2:
        request = _HTTPRequest(
            request.method,
            request.target,
            b"1" if request.minor == b"0" else b"0",
            request.host,
            request.body,
        )
    elif operation == 3:
        request = _HTTPRequest(
            request.method,
            request.target,
            request.minor,
            rng.choice((b"a", b"example.test", b"localhost")),
            request.body,
        )
    else:
        body = bytes(rng.choice(b"abc012 ") for _ in range(rng.randint(0, 16)))
        request = _HTTPRequest(
            rng.choice((b"POST", b"PUT")), request.target, request.minor, request.host, body
        )
    rendered = _render_http(request)
    return rendered if len(rendered) <= max_size else _render_http(_small_http())


def _http_crossover(left: bytes, right: bytes, rng: random.Random, max_size: int) -> bytes:
    first = _parse_http(left)
    second = _parse_http(right)
    if first is None or second is None:
        return _http_generate(rng, max_size)
    request = _HTTPRequest(
        rng.choice((first.method, second.method)),
        rng.choice((first.target, second.target)),
        rng.choice((first.minor, second.minor)),
        rng.choice((first.host, second.host)),
        rng.choice((first.body, second.body)),
    )
    if request.body and request.method in {b"GET", b"HEAD"}:
        request = _HTTPRequest(b"POST", request.target, request.minor, request.host, request.body)
    rendered = _render_http(request)
    return rendered if len(rendered) <= max_size else _render_http(_small_http())


def _http_shrink(data: bytes) -> Iterable[bytes]:
    request = _parse_http(data)
    if request is None:
        return (_render_http(_small_http()),)
    variants = (
        _HTTPRequest(b"GET", request.target, request.minor, request.host, b""),
        _HTTPRequest(b"GET", b"/", request.minor, request.host, b""),
        _HTTPRequest(b"GET", b"/", request.minor, b"a", b""),
        _small_http(),
    )
    return tuple(_render_http(variant) for variant in variants)


def _parse_xml(data: bytes) -> _XMLDocument | None:
    empty = _XML_EMPTY.fullmatch(data)
    if empty is not None:
        return _XMLDocument(empty.group("tag"), None, b"", None, True)
    match = _XML.fullmatch(data)
    if match is None:
        return None
    return _XMLDocument(
        match.group("tag"),
        match.group("identifier"),
        match.group("text"),
        match.group("child"),
    )


def _render_xml(document: _XMLDocument) -> bytes:
    if document.empty:
        return b"<" + document.tag + b"/>"
    parts = [b"<", document.tag]
    if document.identifier is not None:
        parts.extend((b' id="', document.identifier, b'"'))
    parts.extend((b">", document.text))
    if document.child is not None:
        parts.extend((b"<", document.child, b"/>"))
    parts.extend((b"</", document.tag, b">"))
    return b"".join(parts)


def _xml_generate(rng: random.Random, max_size: int) -> bytes:
    tag = rng.choice((b"a", b"root", b"item", b"node"))
    document = _XMLDocument(
        tag,
        rng.choice((None, b"a", b"id0")),
        rng.choice((b"", b"text", b"0", b"alpha beta")),
        rng.choice((None, b"a", b"child", b"item")),
        rng.randrange(5) == 0,
    )
    rendered = _render_xml(document)
    return rendered if len(rendered) <= max_size else b"<a/>"


def _xml_mutate(data: bytes, rng: random.Random, max_size: int) -> bytes:
    document = _parse_xml(data)
    if document is None:
        return _xml_generate(rng, max_size)
    operation = rng.randrange(4)
    if operation == 0:
        document = _XMLDocument(
            rng.choice((b"a", b"root", b"item")), document.identifier, document.text, document.child
        )
    elif operation == 1:
        document = _XMLDocument(
            document.tag, rng.choice((None, b"a", b"id0")), document.text, document.child
        )
    elif operation == 2:
        document = _XMLDocument(
            document.tag, document.identifier, rng.choice((b"", b"text", b"0")), document.child
        )
    else:
        document = _XMLDocument(
            document.tag, document.identifier, document.text, rng.choice((None, b"a", b"child"))
        )
    rendered = _render_xml(document)
    return rendered if len(rendered) <= max_size else b"<a/>"


def _xml_crossover(left: bytes, right: bytes, rng: random.Random, max_size: int) -> bytes:
    first = _parse_xml(left)
    second = _parse_xml(right)
    if first is None or second is None:
        return _xml_generate(rng, max_size)
    document = _XMLDocument(
        rng.choice((first.tag, second.tag)),
        rng.choice((first.identifier, second.identifier)),
        rng.choice((first.text, second.text)),
        rng.choice((first.child, second.child)),
    )
    rendered = _render_xml(document)
    return rendered if len(rendered) <= max_size else b"<a/>"


def _xml_shrink(data: bytes) -> Iterable[bytes]:
    document = _parse_xml(data)
    if document is None:
        return (b"<a/>",)
    return (
        _render_xml(_XMLDocument(document.tag, None, b"", None, True)),
        _render_xml(_XMLDocument(document.tag, None, b"", None)),
        b"<a/>",
    )


HTTP1_STRATEGY = StructuredStrategy(
    "http1-grammar",
    1,
    seeds=(b"GET / HTTP/1.1\r\nHost: a\r\n\r\n",),
    dictionary=(b"GET", b"POST", b"HTTP/1.1", b"Host", b"Content-Length"),
    generate=_http_generate,
    mutate=_http_mutate,
    crossover=_http_crossover,
    shrink=_http_shrink,
)

XML_STRATEGY = StructuredStrategy(
    "xml-grammar",
    1,
    seeds=(b"<a/>", b"<root></root>"),
    dictionary=(b"<", b">", b"/>", b"</", b'id="'),
    generate=_xml_generate,
    mutate=_xml_mutate,
    crossover=_xml_crossover,
    shrink=_xml_shrink,
)

__all__ = [
    "CrossoverHook",
    "DictionaryHook",
    "GenerateHook",
    "HTTP1_STRATEGY",
    "MutateHook",
    "ShrinkHook",
    "StructuredStrategy",
    "XML_STRATEGY",
]
