"""The XML document model and its bounds.

The parser is `_native/xml.c`; it owns a native tree for parsing and
canonicalization, then materializes this `Element` shape once when `parse()`
returns. A second public `Element` would be two trees that agree until they do
not, on a security boundary where a verifier reading one and a consumer reading
the other is the whole shape of a signature-wrapping bug.

`XMLRefusal` is the sharpest case. `wreath.xml` hands this very class to
`_core.xml_configure`, so the C parser raises it rather than minting its own:
the parity suite asserts the reason code *and* the message of every refusal,
and `except XMLRefusal` around a natively parsed document has to catch.

`Limits` and `MAX_DEPTH_CEILING` are shared because a bound only means
something if both arms enforce the same one; the ceiling exists because the C
parser is recursive descent, which is a constraint the pure parser inherits
rather than one it has.
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

#: The largest `max_depth` a caller may ask for. `_native/xml.c` is a recursive
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
    canonicalizer: Callable[..., bytes] | None = field(default=None, repr=False, compare=False)

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
        backend = self.canonicalizer
        if backend is None:
            # Imported here rather than at module scope: `wreath.xml` imports
            # this module for `Element`, so a module-level import back would
            # close a cycle. A document the C parser built carries its own
            # canonicalizer and never reaches this.
            from .xml import canonicalize_span

            backend = canonicalize_span
        return backend(self.source, start, end, target.nsinherited, inclusive_prefixes)


def _walk(element: Element) -> Iterator[Element]:
    yield element
    for child in element.children:
        yield from _walk(child)
