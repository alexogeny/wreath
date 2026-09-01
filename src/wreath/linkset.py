"""Web links serialized as RFC 9264 `application/linkset+json`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from ._json import dumps as _json_dumps
from .response import Response

__all__ = ["LinkContext", "Linkset", "LinksetResponse", "LinkTarget"]


def _uri_reference(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"linkset {name} must be a URI-reference string")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"linkset {name} must not contain spaces or control characters")
    try:
        urlsplit(value)
    except ValueError as error:
        raise ValueError(f"linkset {name} is not a valid URI reference: {error}") from None
    return value


def _relation(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("linkset relation type must be a non-empty string")
    if any(char.isspace() or ord(char) < 0x20 for char in value):
        raise ValueError(f"linkset relation type {value!r} contains whitespace or controls")
    return value


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """One target URI and its Web Linking target attributes."""

    href: str
    hreflang: tuple[str, ...] = ()
    media: str | None = None
    title: str | None = None
    type: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "href", _uri_reference(self.href, "target href"))
        languages = tuple(self.hreflang)
        if any(not isinstance(value, str) or not value for value in languages):
            raise ValueError("linkset target hreflang values must be non-empty strings")
        object.__setattr__(self, "hreflang", languages)
        for name in ("media", "title", "type"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"linkset target {name} must be a string or None")
        extras = dict(self.attributes)
        reserved = {"href", "hreflang", "media", "title", "type"}.intersection(extras)
        if reserved:
            raise ValueError(
                "linkset target extension attributes must not replace "
                + ", ".join(sorted(reserved))
            )
        if any(not isinstance(name, str) or not name for name in extras):
            raise ValueError("linkset target extension attribute names must be non-empty strings")
        object.__setattr__(self, "attributes", MappingProxyType(extras))

    def document(self) -> dict[str, Any]:
        target: dict[str, Any] = {"href": self.href}
        if self.hreflang:
            target["hreflang"] = list(self.hreflang)
        for name in ("media", "title", "type"):
            value = getattr(self, name)
            if value is not None:
                target[name] = value
        target.update(self.attributes)
        return target

    @classmethod
    def from_document(cls, document: Any) -> LinkTarget:
        if not isinstance(document, Mapping) or "href" not in document:
            raise ValueError("linkset target must be an object containing href")
        hreflang = document.get("hreflang", [])
        if not isinstance(hreflang, list) or any(not isinstance(item, str) for item in hreflang):
            raise ValueError("linkset target hreflang must be an array of strings")
        known = {"href", "hreflang", "media", "title", "type"}
        return cls(
            href=document["href"],
            hreflang=tuple(hreflang),
            media=document.get("media"),
            title=document.get("title"),
            type=document.get("type"),
            attributes={name: value for name, value in document.items() if name not in known},
        )


@dataclass(frozen=True, slots=True)
class LinkContext:
    """Links sharing one context URI, grouped by relation type."""

    anchor: str | None
    links: Mapping[str, tuple[LinkTarget, ...]]

    def __post_init__(self) -> None:
        if self.anchor is not None:
            object.__setattr__(self, "anchor", _uri_reference(self.anchor, "anchor"))
        normalized: dict[str, tuple[LinkTarget, ...]] = {}
        for raw_relation, raw_targets in self.links.items():
            relation = _relation(raw_relation)
            targets = tuple(raw_targets)
            if not targets or any(not isinstance(target, LinkTarget) for target in targets):
                raise ValueError(
                    f"linkset relation {relation!r} must contain one or more LinkTarget values"
                )
            normalized[relation] = targets
        if not normalized:
            raise ValueError("linkset context must contain at least one relation")
        object.__setattr__(self, "links", MappingProxyType(normalized))

    def document(self) -> dict[str, Any]:
        context: dict[str, Any] = {}
        if self.anchor is not None:
            context["anchor"] = self.anchor
        context.update(
            {
                relation: [target.document() for target in targets]
                for relation, targets in self.links.items()
            }
        )
        return context

    @classmethod
    def from_document(cls, document: Any) -> LinkContext:
        if not isinstance(document, Mapping):
            raise ValueError("linkset context must be an object")
        links: dict[str, tuple[LinkTarget, ...]] = {}
        for name, targets in document.items():
            if name == "anchor":
                continue
            if not isinstance(targets, list):
                raise ValueError(f"linkset relation {name!r} must be an array of targets")
            links[name] = tuple(LinkTarget.from_document(target) for target in targets)
        return cls(anchor=document.get("anchor"), links=links)


@dataclass(frozen=True, slots=True, init=False)
class Linkset:
    """A set of links grouped into RFC 9264 link-context objects."""

    contexts: tuple[LinkContext, ...]

    def __init__(self, *contexts: LinkContext) -> None:
        if any(not isinstance(context, LinkContext) for context in contexts):
            raise ValueError("linkset contexts must be LinkContext values")
        object.__setattr__(self, "contexts", tuple(contexts))

    def document(self) -> dict[str, Any]:
        return {"linkset": [context.document() for context in self.contexts]}

    @classmethod
    def from_document(cls, document: Any) -> Linkset:
        if not isinstance(document, Mapping) or set(document) != {"linkset"}:
            raise ValueError("linkset document must contain linkset as its sole member")
        contexts = document["linkset"]
        if not isinstance(contexts, list):
            raise ValueError("linkset member must be an array")
        return cls(*(LinkContext.from_document(context) for context in contexts))


class LinksetResponse(Response):
    """A UTF-8 RFC 9264 JSON linkset response."""

    media_type = b"application/linkset+json"

    def __init__(
        self,
        linkset: Linkset,
        *,
        profile: str | None = None,
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
    ) -> None:
        media_type = self.media_type
        if profile is not None:
            _uri_reference(profile, "profile")
            try:
                encoded_profile = profile.encode("ascii")
            except UnicodeEncodeError:
                raise ValueError("linkset profile URI must contain only ASCII") from None
            if b'"' in encoded_profile or b"\\" in encoded_profile:
                raise ValueError("linkset profile URI must not contain quotes or backslashes")
            media_type += b'; profile="' + encoded_profile + b'"'
        super().__init__(
            _json_dumps(linkset.document()),
            status=status,
            headers=headers,
            media_type=media_type,
        )
