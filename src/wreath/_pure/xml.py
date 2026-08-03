"""Pure-Python twin of the strict XML parser and exclusive canonicalizer.

The C implementation in `wreath._native.xml` is the one a normal build runs;
this is what `WREATH_PURE=1` selects, and it is the reference the C is held
to byte for byte by `tests/test_xml_parity.py`.

Two implementations of one parser is ordinarily a liability on a security
boundary -- a verifier running one and a consumer running the other is the
shape of a signature-wrapping bug. The parity suite is what makes it safe:
both are driven over the same corpus, including every exploit, and must agree
on the tree, on the byte spans, on the canonical bytes, and on the reason for
every refusal.

Everything here works on `bytes` rather than on a decoded `str`, because
the byte offsets are the point: a caller verifies a signature over the exact
source bytes of a subtree, so a character offset would be the wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
XMLNS_NAMESPACE = "http://www.w3.org/2000/xmlns/"

#: Attribute names `find_id` treats as carrying an element identifier. There
#: is no DTD to type an attribute as ID, so the set is declared rather than
#: derived: `ID` is what SAML and XML-DSig use, `xml:id` is the W3C
#: spelling. Anything else is an ordinary attribute.
ID_ATTRIBUTES = ("ID", f"{{{XML_NAMESPACE}}}id")

#: The largest `max_depth` a caller may ask for. The C twin is a recursive
#: descent parser, so an unbounded depth would be a caller-selectable way to
#: exhaust the C stack -- a segfault rather than a refusal. Kept in step with
#: `XML_MAX_DEPTH_CEILING` in `_native/xml.c`.
MAX_DEPTH_CEILING = 1000


class XMLRefusal(ValueError):
    """A document outside the accepted profile.

    `reason` is a stable machine-readable code; the message says which
    construct was refused and where. Both are part of the contract, because a
    refusal test that asserts only the exception type passes on whichever
    branch happened to fire.
    """

    __slots__ = ("offset", "reason")

    def __init__(self, reason: str, message: str, offset: int) -> None:
        super().__init__(f"{message} (at byte {offset})")
        self.reason = reason
        self.offset = offset


@dataclass(frozen=True, slots=True)
class Limits:
    """Bounds every parse is checked against.

    The defaults suit a SAML assertion or an S3 response. There is no unbounded
    setting: a parser on an unauthenticated boundary that will read whatever it
    is given has no answer to a document built to exhaust memory.
    """

    max_bytes: int = 1 << 20
    max_depth: int = 100
    max_elements: int = 65_536
    max_attributes: int = 256
    max_attribute_bytes: int = 65_536

    def __post_init__(self) -> None:
        for name in (
            "max_bytes",
            "max_depth",
            "max_elements",
            "max_attributes",
            "max_attribute_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer, not {value!r}")
        if self.max_depth > MAX_DEPTH_CEILING:
            raise ValueError(
                f"max_depth must not exceed {MAX_DEPTH_CEILING}, the recursion "
                "ceiling the C parser is bounded by"
            )


@dataclass(frozen=True, slots=True)
class Element:
    """One element, and the byte range of the source it was parsed from.

    `tag` and the keys of `attrib` use the `{uri}local` spelling
    `xml.etree.ElementTree` uses, so a caller reading values out of this tree
    reads the same strings it would from the stdlib.
    """

    tag: str
    attrib: dict[str, str]
    text: str
    tail: str
    children: tuple[Element, ...]
    span: tuple[int, int]
    nsdeclarations: tuple[tuple[str, str], ...]
    #: Every binding in scope here, not only those declared on this element.
    #: Shared by identity with the parent when this element declares nothing,
    #: so the memory cost is one tuple per distinct scope rather than per node.
    nsscope: tuple[tuple[str, str], ...] = field(repr=False, default=())
    #: The scope the *parent* established, which is what canonicalizing this
    #: subtree from its source bytes has to be seeded with.
    nsinherited: tuple[tuple[str, str], ...] = field(repr=False, default=())
    #: `(prefix, local, uri, value)` per attribute, in document order. The
    #: expanded `attrib` mapping loses the prefix, and canonicalization has
    #: to write the prefix back out.
    qualified: tuple[tuple[str, str, str, str], ...] = field(repr=False, default=())
    prefix: str = field(repr=False, default="")
    local: str = field(repr=False, default="")

    def __iter__(self) -> Iterator[Element]:
        return iter(self.children)

    def __len__(self) -> int:
        return len(self.children)


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed document, and the bytes it was parsed from."""

    root: Element
    source: bytes
    #: Which canonicalizer this document's `canonicalize` uses. The
    #: facade passes the C one in when it parsed with C, so a document carries
    #: its backend explicitly rather than the module reaching for a global that
    #: some other import may have swapped.
    canonicalizer: Callable[..., bytes] | None = field(
        default=None, repr=False, compare=False
    )

    def subtree_bytes(self, element: Element) -> bytes:
        """The original bytes of `element`, start tag through end tag.

        This is the provenance that makes signature wrapping a non-issue: a
        caller verifies over these bytes and reads values from this same
        element, so there is no second lookup for an attacker to divert.
        """
        start, end = element.span
        return self.source[start:end]

    def find_id(self, value: str) -> Element | None:
        """The element whose `ID` is `value`, or `None`.

        Refuses when more than one element carries the identifier. A document
        with two candidates for one ID is the precondition for every signature-
        wrapping attack, so it is rejected rather than resolved by document
        order.
        """
        found = [
            element
            for element in _walk(self.root)
            if any(element.attrib.get(name) == value for name in ID_ATTRIBUTES)
        ]
        if len(found) > 1:
            raise XMLRefusal(
                "duplicate-id",
                f"the identifier {value!r} appears {len(found)} times; a document "
                "with two candidates for one identifier is refused",
                found[1].span[0],
            )
        return found[0] if found else None

    def canonicalize(
        self,
        element: Element | None = None,
        inclusive_prefixes: Sequence[str] = (),
    ) -> bytes:
        """Exclusive XML Canonicalization 1.0 of `element` (default: root).

        `inclusive_prefixes` is the `InclusiveNamespaces` PrefixList: those
        prefixes are rendered when in scope even if the subtree does not
        visibly utilize them. `#default` names the default namespace.

        The canonical form is computed by **re-reading the subtree's own source
        bytes**, not by re-serializing the tree this document already holds.
        That is what makes the bytes a signature covers and the bytes a
        consumer reads provably the same: there is no reconstruction step in
        between for the two to disagree across.
        """
        target = element if element is not None else self.root
        start, end = target.span
        backend = self.canonicalizer or canonicalize_span
        return backend(
            self.source, start, end, target.nsinherited, inclusive_prefixes
        )


def _walk(element: Element) -> Iterator[Element]:
    yield element
    for child in element.children:
        yield from _walk(child)


# ---------------------------------------------------------------------------
# Name characters -- the XML 1.0 fifth-edition productions, spelled out so the
# C twin can implement exactly the same ranges rather than an approximation.
# ---------------------------------------------------------------------------

_START_RANGES = (
    (0xC0, 0xD6),
    (0xD8, 0xF6),
    (0xF8, 0x2FF),
    (0x370, 0x37D),
    (0x37F, 0x1FFF),
    (0x200C, 0x200D),
    (0x2070, 0x218F),
    (0x2C00, 0x2FEF),
    (0x3001, 0xD7FF),
    (0xF900, 0xFDCF),
    (0xFDF0, 0xFFFD),
    (0x10000, 0xEFFFF),
)
_EXTRA_RANGES = ((0x300, 0x36F), (0x203F, 0x2040))


def _is_name_start(cp: int) -> bool:
    if cp < 0x80:
        return cp == 0x3A or cp == 0x5F or (0x41 <= cp <= 0x5A) or (0x61 <= cp <= 0x7A)
    return any(low <= cp <= high for low, high in _START_RANGES)


def _is_name_char(cp: int) -> bool:
    if cp < 0x80:
        return (
            _is_name_start(cp)
            or cp == 0x2D
            or cp == 0x2E
            or (0x30 <= cp <= 0x39)
        )
    if cp == 0xB7:
        return True
    return _is_name_start(cp) or any(low <= cp <= high for low, high in _EXTRA_RANGES)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

_WHITESPACE = b" \t\r\n"


class _Node:
    """Mutable element under construction.

    An element's `tail` is text that follows it, so it is not known until the
    next sibling or the parent's end tag is reached. Building into a mutable
    node and freezing the whole tree once at the end is the alternative to
    making `Element` mutable for the sake of that one field.
    """

    __slots__ = (
        "attrib",
        "children",
        "local",
        "nsdecl",
        "nsinherited",
        "nsscope",
        "prefix",
        "qualified",
        "span",
        "tag",
        "tail",
        "text",
    )

    def __init__(self) -> None:
        self.tag = ""
        self.attrib: dict[str, str] = {}
        self.text = ""
        self.tail = ""
        self.children: list[_Node] = []
        self.span = (0, 0)
        self.nsdecl: tuple[tuple[str, str], ...] = ()
        self.nsscope: tuple[tuple[str, str], ...] = ()
        self.nsinherited: tuple[tuple[str, str], ...] = ()
        self.qualified: tuple[tuple[str, str, str, str], ...] = ()
        self.prefix = ""
        self.local = ""


def _freeze_tree(node: _Node) -> Element:
    return Element(
        tag=node.tag,
        attrib=node.attrib,
        text=node.text,
        tail=node.tail,
        children=tuple(_freeze_tree(child) for child in node.children),
        span=node.span,
        nsdeclarations=node.nsdecl,
        nsscope=node.nsscope,
        nsinherited=node.nsinherited,
        qualified=node.qualified,
        prefix=node.prefix,
        local=node.local,
    )


class _Parser:
    __slots__ = ("data", "elements", "limits", "pos", "scopes")

    def __init__(
        self,
        data: bytes,
        limits: Limits,
        initial_scope: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.data = data
        self.limits = limits
        self.pos = 0
        self.elements = 0
        # Seeded when canonicalizing a subtree from its own source bytes: the
        # span does not carry the ancestor declarations it inherits.
        self.scopes: list[tuple[tuple[str, str], ...]] = (
            [initial_scope] if initial_scope else []
        )

    def fail(self, reason: str, message: str, offset: int | None = None) -> XMLRefusal:
        return XMLRefusal(reason, message, self.pos if offset is None else offset)

    # -- byte level ------------------------------------------------------

    def validate_bytes(self) -> None:
        """UTF-8 well-formedness and the legal character range, in one pass.

        Doing this up front means no later stage can be handed a byte sequence
        that decodes two ways, which is the whole family of encoding-confusion
        tricks. It also means the tree builder never has to handle a decode
        error halfway through an element.
        """
        data = self.data
        length = len(data)
        i = 0
        while i < length:
            byte = data[i]
            if byte < 0x80:
                if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D):
                    raise XMLRefusal(
                        "control-character",
                        f"control character 0x{byte:02X} is not permitted in XML",
                        i,
                    )
                i += 1
                continue
            if 0xC2 <= byte <= 0xDF:
                width, cp = 2, byte & 0x1F
            elif 0xE0 <= byte <= 0xEF:
                width, cp = 3, byte & 0x0F
            elif 0xF0 <= byte <= 0xF4:
                width, cp = 4, byte & 0x07
            else:
                raise XMLRefusal(
                    "encoding",
                    f"byte 0x{byte:02X} does not start a valid UTF-8 sequence",
                    i,
                )
            if i + width > length:
                raise XMLRefusal(
                    "encoding", "the document ends inside a UTF-8 sequence", i
                )
            for offset in range(1, width):
                cont = data[i + offset]
                if cont & 0xC0 != 0x80:
                    raise XMLRefusal(
                        "encoding",
                        f"byte 0x{cont:02X} is not a UTF-8 continuation byte",
                        i + offset,
                    )
                cp = (cp << 6) | (cont & 0x3F)
            lowest = (0x80, 0x800, 0x10000)[width - 2]
            if cp < lowest:
                raise XMLRefusal(
                    "encoding",
                    f"U+{cp:04X} is written as an overlong UTF-8 sequence",
                    i,
                )
            if 0xD800 <= cp <= 0xDFFF:
                raise XMLRefusal(
                    "encoding",
                    f"U+{cp:04X} is a surrogate and cannot appear in UTF-8",
                    i,
                )
            if cp > 0x10FFFF or cp in (0xFFFE, 0xFFFF):
                raise XMLRefusal(
                    "encoding", f"U+{cp:04X} is not a valid XML character", i
                )
            i += width

    def decode_at(self, index: int) -> tuple[int, int]:
        """Codepoint and width at `index`; the buffer is already validated."""
        data = self.data
        byte = data[index]
        if byte < 0x80:
            return byte, 1
        if byte < 0xE0:
            return ((byte & 0x1F) << 6) | (data[index + 1] & 0x3F), 2
        if byte < 0xF0:
            return (
                ((byte & 0x0F) << 12)
                | ((data[index + 1] & 0x3F) << 6)
                | (data[index + 2] & 0x3F)
            ), 3
        return (
            ((byte & 0x07) << 18)
            | ((data[index + 1] & 0x3F) << 12)
            | ((data[index + 2] & 0x3F) << 6)
            | (data[index + 3] & 0x3F)
        ), 4

    # -- lexical ---------------------------------------------------------

    def skip_whitespace(self) -> None:
        data = self.data
        while self.pos < len(data) and data[self.pos] in _WHITESPACE:
            self.pos += 1

    def read_name(self) -> str:
        data = self.data
        start = self.pos
        first = True
        while self.pos < len(data):
            byte = data[self.pos]
            if byte < 0x80:
                cp, width = byte, 1
                if not (_is_name_start(cp) if first else _is_name_char(cp)):
                    break
            else:
                cp, width = self.decode_at(self.pos)
                if not (_is_name_start(cp) if first else _is_name_char(cp)):
                    raise self.fail(
                        "invalid-name",
                        f"U+{cp:04X} cannot appear in an XML name",
                    )
            first = False
            self.pos += width
        if self.pos == start:
            raise self.fail("invalid-name", "an XML name was expected here")
        return data[start : self.pos].decode()

    def split_name(self, name: str) -> tuple[str, str]:
        prefix, sep, local = name.partition(":")
        if not sep:
            return "", name
        if not local or ":" in local:
            raise self.fail(
                "invalid-name", f"{name!r} is not a valid qualified name"
            )
        return prefix, local

    def read_reference(self, into: list[str]) -> None:
        """Consume one `&...;` and append what it stands for."""
        data = self.data
        start = self.pos
        self.pos += 1
        if self.pos < len(data) and data[self.pos] == 0x23:  # '#'
            self.pos += 1
            hexadecimal = self.pos < len(data) and data[self.pos] in (0x78, 0x58)
            if hexadecimal:
                self.pos += 1
            digits_start = self.pos
            while self.pos < len(data) and data[self.pos] != 0x3B:
                self.pos += 1
            if self.pos >= len(data):
                raise self.fail(
                    "character-reference",
                    "the document ends inside a character reference",
                    start,
                )
            digits = data[digits_start : self.pos].decode()
            self.pos += 1
            try:
                cp = int(digits, 16 if hexadecimal else 10)
            except ValueError:
                raise self.fail(
                    "character-reference",
                    f"{digits!r} is not a valid character reference",
                    start,
                ) from None
            if (
                cp > 0x10FFFF
                or (0xD800 <= cp <= 0xDFFF)
                or (cp < 0x20 and cp not in (0x09, 0x0A, 0x0D))
                or cp in (0xFFFE, 0xFFFF)
            ):
                raise self.fail(
                    "character-reference",
                    f"character reference U+{cp:04X} is not a valid XML character",
                    start,
                )
            into.append(chr(cp))
            return
        end = data.find(b";", self.pos)
        if end < 0:
            raise self.fail(
                "entity-reference", "the document ends inside an entity reference", start
            )
        name = data[self.pos : end].decode(errors="replace")
        self.pos = end + 1
        expansion = {
            "lt": "<",
            "gt": ">",
            "amp": "&",
            "quot": '"',
            "apos": "'",
        }.get(name)
        if expansion is None:
            raise self.fail(
                "entity-reference",
                f"entity reference &{name}; is not one of the five XML "
                "predefined entities, and this parser declares none",
                start,
            )
        into.append(expansion)

    def read_attribute_value(self) -> str:
        data = self.data
        quote = data[self.pos] if self.pos < len(data) else 0
        if quote not in (0x22, 0x27):
            raise self.fail(
                "attribute-syntax", "an attribute value must be quoted"
            )
        self.pos += 1
        start = self.pos
        parts: list[str] = []
        run = self.pos
        while True:
            if self.pos >= len(data):
                raise self.fail(
                    "unexpected-end", "the document ends inside an attribute value", start
                )
            byte = data[self.pos]
            if byte == quote:
                parts.append(data[run : self.pos].decode())
                self.pos += 1
                break
            if byte == 0x3C:  # '<'
                raise self.fail(
                    "attribute-syntax", "'<' is not permitted in an attribute value"
                )
            if byte == 0x26:  # '&'
                parts.append(data[run : self.pos].decode())
                self.read_reference(parts)
                run = self.pos
                continue
            if byte in (0x09, 0x0A, 0x0D):
                # Attribute-value normalization: literal whitespace becomes a
                # space. A character reference does not, which is the whole
                # reason the escape exists.
                parts.append(data[run : self.pos].decode())
                parts.append(" ")
                self.pos += 1
                if byte == 0x0D and self.pos < len(data) and data[self.pos] == 0x0A:
                    self.pos += 1
                run = self.pos
                continue
            self.pos += 1
        value = "".join(parts)
        if self.pos - start > self.limits.max_attribute_bytes:
            raise self.fail(
                "attribute-size",
                f"attribute value exceeds the {self.limits.max_attribute_bytes}-byte "
                "limit",
                start,
            )
        return value

    # -- document --------------------------------------------------------

    def parse(self) -> Document:
        data = self.data
        if not data:
            raise XMLRefusal("unexpected-end", "the document is empty", 0)
        if len(data) > self.limits.max_bytes:
            raise XMLRefusal(
                "size",
                f"document size {len(data)} exceeds the "
                f"{self.limits.max_bytes}-byte limit",
                0,
            )
        if data.startswith(b"\xef\xbb\xbf"):
            raise XMLRefusal(
                "byte-order-mark",
                "a byte order mark is outside this profile; the bytes a "
                "signature covers must be the bytes that were parsed",
                0,
            )
        self.validate_bytes()
        self.read_declaration()

        root: _Node | None = None
        while self.pos < len(data):
            byte = data[self.pos]
            if byte in _WHITESPACE:
                self.pos += 1
                continue
            if byte != 0x3C:
                raise self.fail(
                    "content-before-root" if root is None else "trailing-content",
                    "character data is not permitted outside the root element",
                )
            self.reject_markup_declaration()
            if root is not None:
                raise self.fail(
                    "trailing-content", "content is not permitted after the root element"
                )
            root = self.read_element(depth=1)
        if root is None:
            raise XMLRefusal(
                "unexpected-end", "the document has no root element", len(data)
            )
        return Document(root=_freeze_tree(root), source=data)

    def read_declaration(self) -> None:
        data = self.data
        if not data.startswith(b"<?xml"):
            return
        if len(data) > 5 and data[5] not in _WHITESPACE:
            return  # `<?xmlfoo` is a processing instruction, refused below
        end = data.find(b"?>", 5)
        if end < 0:
            raise XMLRefusal(
                "unexpected-end", "the XML declaration is not terminated", 0
            )
        body = data[5:end].decode()
        self.pos = end + 2
        version = _pseudo_attribute(body, "version")
        if version is None or version != "1.0":
            raise XMLRefusal(
                "version",
                f"XML version {version!r} is not supported; this parser is XML 1.0",
                0,
            )
        encoding = _pseudo_attribute(body, "encoding")
        if encoding is not None and encoding.lower() != "utf-8":
            raise XMLRefusal(
                "encoding",
                f"declared encoding {encoding!r} is refused; this parser reads UTF-8",
                0,
            )

    def reject_markup_declaration(self) -> None:
        """Refuse everything spelled `<!...` or `<?...` at the current position."""
        data = self.data
        rest = data[self.pos : self.pos + 9]
        if rest.startswith(b"<!--"):
            raise self.fail(
                "comment",
                "comments are refused: a comment splits a text node, and two "
                "readings of one value is how a signed assertion is truncated",
            )
        if rest.startswith(b"<![CDATA["):
            raise self.fail(
                "cdata",
                "CDATA sections are refused: they are a second spelling of text, "
                "and one value with two spellings is an ambiguity a signature "
                "cannot resolve",
            )
        if rest.startswith(b"<!DOCTYPE"):
            raise self.fail(
                "doctype",
                "a document type declaration is refused: it is the only way to "
                "declare an entity, and therefore the only way to reach an "
                "expander or an external resolver",
            )
        if rest.startswith(b"<!"):
            raise self.fail(
                "markup-declaration", "markup declarations are refused"
            )
        if rest.startswith(b"<?"):
            raise self.fail(
                "processing-instruction",
                "processing instructions are refused; only an XML declaration "
                "at the start of the document is accepted",
            )

    def read_element(self, depth: int) -> _Node:
        data = self.data
        if depth > self.limits.max_depth:
            raise self.fail(
                "depth", f"nesting depth exceeds the {self.limits.max_depth} limit"
            )
        self.elements += 1
        if self.elements > self.limits.max_elements:
            raise self.fail(
                "elements", f"element count exceeds the {self.limits.max_elements} limit"
            )
        start = self.pos
        self.pos += 1  # '<'
        name = self.read_name()

        raw_attributes: list[tuple[str, str, int]] = []
        declarations: list[tuple[str, str]] = []
        while True:
            before = self.pos
            self.skip_whitespace()
            if self.pos >= len(data):
                raise self.fail(
                    "unexpected-end", "the document ends inside a start tag", start
                )
            byte = data[self.pos]
            if byte == 0x3E:  # '>'
                self.pos += 1
                empty = False
                break
            if byte == 0x2F:  # '/'
                if self.pos + 1 >= len(data) or data[self.pos + 1] != 0x3E:
                    raise self.fail("tag-syntax", "'/' must be followed by '>'")
                self.pos += 2
                empty = True
                break
            if self.pos == before:
                raise self.fail(
                    "tag-syntax", "whitespace is required between attributes"
                )
            attribute_name = self.read_name()
            self.skip_whitespace()
            if self.pos >= len(data) or data[self.pos] != 0x3D:
                raise self.fail("attribute-syntax", "an attribute needs a value")
            self.pos += 1
            self.skip_whitespace()
            value = self.read_attribute_value()
            raw_attributes.append((attribute_name, value, before))
            if len(raw_attributes) > self.limits.max_attributes:
                raise self.fail(
                    "attributes",
                    f"attribute count exceeds the {self.limits.max_attributes} limit",
                )

        for attribute_name, value, offset in raw_attributes:
            if attribute_name == "xmlns":
                declarations.append(("", value))
            elif attribute_name.startswith("xmlns:"):
                prefix = attribute_name[6:]
                if not prefix or ":" in prefix:
                    raise self.fail(
                        "invalid-name", f"{attribute_name!r} is not a valid declaration"
                    )
                if prefix in ("xmlns", "xml") and value != (
                    XML_NAMESPACE if prefix == "xml" else ""
                ):
                    raise self.fail(
                        "reserved-prefix",
                        f"the {prefix!r} prefix is reserved and cannot be rebound",
                        offset,
                    )
                if not value:
                    raise self.fail(
                        "empty-prefix-uri",
                        f"a prefix cannot be bound to the empty namespace; "
                        f"undeclaring {prefix!r} is an XML 1.1 feature",
                        offset,
                    )
                declarations.append((prefix, value))

        return self.build_element(name, raw_attributes, declarations, start, empty, depth)

    def build_element(
        self,
        name: str,
        raw_attributes: list[tuple[str, str, int]],
        declarations: list[tuple[str, str]],
        start: int,
        empty: bool,
        depth: int,
    ) -> _Node:
        inherited = self.scopes[-1] if self.scopes else ()
        scope = dict(inherited)
        for prefix, uri in declarations:
            if uri:
                scope[prefix] = uri
            else:
                scope.pop(prefix, None)
        # Shared by identity when this element declares nothing, so the memory
        # cost is one tuple per distinct scope rather than one per element.
        scope_tuple = inherited if not declarations else _freeze(scope)

        prefix, local = self.split_name(name)
        uri = self.resolve(prefix, scope)
        tag = f"{{{uri}}}{local}" if uri else local

        attrib: dict[str, str] = {}
        qualified: list[tuple[str, str, str, str]] = []
        for attribute_name, value, offset in raw_attributes:
            if attribute_name == "xmlns" or attribute_name.startswith("xmlns:"):
                continue
            attribute_prefix, attribute_local = self.split_name(attribute_name)
            attribute_uri = (
                self.resolve(attribute_prefix, scope, offset)
                if attribute_prefix
                else ""
            )
            key = (
                f"{{{attribute_uri}}}{attribute_local}"
                if attribute_uri
                else attribute_local
            )
            if key in attrib:
                raise self.fail(
                    "duplicate-attribute",
                    f"duplicate attribute {key!r} on one element",
                    offset,
                )
            attrib[key] = value
            qualified.append((attribute_prefix, attribute_local, attribute_uri, value))

        node = _Node()
        if not empty:
            self.scopes.append(scope_tuple)
            try:
                node.text, node.children = self.read_content(name, depth)
            finally:
                self.scopes.pop()

        node.tag = tag
        node.attrib = attrib
        node.span = (start, self.pos)
        node.nsdecl = tuple(declarations)
        node.nsscope = scope_tuple
        node.nsinherited = inherited
        node.qualified = tuple(qualified)
        node.prefix = prefix
        node.local = local
        return node

    def resolve(
        self,
        prefix: str,
        scope: dict[str, str],
        offset: int | None = None,
    ) -> str:
        """Expand `prefix` against `scope`.

        Only ever called with an empty prefix for an *element* name: an
        unprefixed attribute is in no namespace, so `build_element` answers
        that case without asking. A second `element=` spelling of the same
        rule used to live here and was dead on one arm.
        """
        if prefix == "xml":
            return XML_NAMESPACE
        if not prefix:
            return scope.get("", "")
        uri = scope.get(prefix)
        if uri is None:
            raise self.fail(
                "unbound-prefix",
                f"unbound namespace prefix {prefix!r}",
                offset,
            )
        return uri

    def read_content(self, name: str, depth: int) -> tuple[str, list[_Node]]:
        data = self.data
        children: list[_Node] = []
        text_parts: list[str] = []
        pending: list[tuple[_Node, list[str]]] = []
        # Character data accumulates into this element's `text` until the first
        # child, and into each child's `tail` after that.
        target = text_parts
        run = self.pos
        while True:
            if self.pos >= len(data):
                raise self.fail(
                    "mismatched-end-tag",
                    f"the document ends before </{name}>",
                )
            byte = data[self.pos]
            if byte == 0x26:  # '&'
                target.append(data[run : self.pos].decode())
                self.read_reference(target)
                run = self.pos
                continue
            if byte == 0x0D:
                target.append(data[run : self.pos].decode())
                target.append("\n")
                self.pos += 1
                if self.pos < len(data) and data[self.pos] == 0x0A:
                    self.pos += 1
                run = self.pos
                continue
            if byte != 0x3C:  # '<'
                self.pos += 1
                continue
            target.append(data[run : self.pos].decode())
            if data[self.pos : self.pos + 2] == b"</":
                closing = self.pos
                self.pos += 2
                end_name = self.read_name()
                self.skip_whitespace()
                if self.pos >= len(data) or data[self.pos] != 0x3E:
                    raise self.fail("tag-syntax", "an end tag must finish with '>'")
                self.pos += 1
                if end_name != name:
                    raise self.fail(
                        "mismatched-end-tag",
                        f"end tag </{end_name}> does not match <{name}>",
                        closing,
                    )
                break
            self.reject_markup_declaration()
            child = self.read_element(depth + 1)
            children.append(child)
            tail_parts: list[str] = []
            pending.append((child, tail_parts))
            target = tail_parts
            run = self.pos

        for child, tail_parts in pending:
            child.tail = "".join(tail_parts)
        return "".join(text_parts), children


def _freeze(scope: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(scope.items()))


def _pseudo_attribute(body: str, name: str) -> str | None:
    index = body.find(name)
    if index < 0:
        return None
    rest = body[index + len(name) :].lstrip()
    if not rest.startswith("="):
        return None
    rest = rest[1:].lstrip()
    if not rest or rest[0] not in "\"'":
        return None
    quote = rest[0]
    end = rest.find(quote, 1)
    return None if end < 0 else rest[1:end]


def parse_document(data: bytes, limits: Limits | None = None) -> Document:
    """Parse `data` under `limits`, or raise `XMLRefusal`."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("XML input must be bytes")
    return _Parser(bytes(data), limits or Limits()).parse()


# ---------------------------------------------------------------------------
# Exclusive XML Canonicalization 1.0
# ---------------------------------------------------------------------------


def _escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "&#xD;")
    )


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("\t", "&#x9;")
        .replace("\n", "&#xA;")
        .replace("\r", "&#xD;")
    )


def canonicalize(element: Element, inclusive_prefixes: Sequence[str] = ()) -> bytes:
    """Exclusive c14n of an already-parsed subtree."""
    inclusive = {"" if name == "#default" else name for name in inclusive_prefixes}
    out: list[str] = []
    _render(element, {}, inclusive, out)
    return "".join(out).encode()


def canonicalize_span(
    data: bytes,
    start: int,
    end: int,
    inherited: Sequence[tuple[str, str]] = (),
    inclusive_prefixes: Sequence[str] = (),
    limits: Limits | None = None,
) -> bytes:
    """Exclusive c14n of `data[start:end]`, seeded with `inherited`.

    Re-parsing the span rather than re-serializing a tree is deliberate: the
    input to canonicalization is then the same bytes a detached signature was
    computed over.
    """
    if not 0 <= start < end <= len(data):
        raise ValueError("span does not address the source")
    parser = _Parser(data[start:end], limits or Limits(), tuple(inherited))
    return canonicalize(parser.parse().root, inclusive_prefixes)


def _render(
    element: Element,
    rendered: dict[str, str],
    inclusive: set[str],
    out: list[str],
) -> None:
    scope = dict(element.nsscope)
    utilized = {element.prefix}
    utilized.update(prefix for prefix, _, _, _ in element.qualified if prefix)
    utilized.update(prefix for prefix in inclusive if prefix in scope)
    utilized.discard("xml")

    emitted: list[tuple[str, str]] = []
    for prefix in sorted(utilized):
        uri = scope.get(prefix, "")
        previous = rendered.get(prefix, "")
        if uri == previous:
            continue
        if not uri and not previous:
            continue
        emitted.append((prefix, uri))

    qualified_name = (
        f"{element.prefix}:{element.local}" if element.prefix else element.local
    )
    out.append(f"<{qualified_name}")
    for prefix, uri in emitted:
        declaration = f"xmlns:{prefix}" if prefix else "xmlns"
        out.append(f' {declaration}="{_escape_attribute(uri)}"')

    # Attributes sort by (namespace URI, local name), so the no-namespace ones
    # -- whose URI is the empty string -- come first.
    for prefix, local, _, value in sorted(
        element.qualified, key=lambda item: (item[2], item[1])
    ):
        name = f"{prefix}:{local}" if prefix else local
        out.append(f' {name}="{_escape_attribute(value)}"')
    out.append(">")

    child_rendered = dict(rendered)
    child_rendered.update(emitted)

    out.append(_escape_text(element.text))
    for child in element.children:
        _render(child, child_rendered, inclusive, out)
        out.append(_escape_text(child.tail))
    out.append(f"</{qualified_name}>")
