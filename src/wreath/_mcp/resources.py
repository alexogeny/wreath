"""Declared resources: what a model may read, and what it may be told about.

A resource is the half of MCP that is *not* an action. A tool is something a
model does; a resource is something it reads, addressed by a URI it got from
`resources/list` rather than by a name and an argument object. That difference
is the whole reason the two are separate here: a resource has no arguments, so
it has no schema, and its identity is a URI the client can hold on to across
calls and subscribe to.

**The URI is the Cedar resource.** A declared resource gated with `action=` is
decided against its own URI, so there is no second identifier to pass and no way
for the two to disagree. That is the one place this differs from a tool, where
the resource has to be resolved from the call's arguments because a tool has no
stable identity of its own.

Reading is deliberately narrow: the handler takes the request and returns text,
bytes, or a value to render as JSON. Templated resources -- a URI with a
placeholder in it, resolved per read -- are not declarable, and
`resources/templates/list` says so by returning nothing rather than by failing.
"""

from __future__ import annotations

import base64
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .._auth.requirements import AuthRequirement
from .._json import dumps as _json_dumps
from .catalog import Catalog
from .registry import NO_REQUIREMENT, policy_requirement

#: What a reader's return value is served as when the declaration says nothing.
#: Text and bytes differ in kind rather than in encoding -- one is a `text`
#: content block and the other a base64 `blob` -- so they get different defaults.
TEXT_MEDIA_TYPE = "text/plain"
BINARY_MEDIA_TYPE = "application/octet-stream"
JSON_MEDIA_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class Resource:
    """One declared resource: what `resources/list` renders and `read` serves.

    Attributes:
        uri: The identifier a client reads, subscribes to, and sees again on
            every notification about it. Unique within one `MCP`.
        name: A short programmatic name.
        description: What this resource holds, and when a model should read it.
            Never empty, for the same reason a tool's is not.
        handler: The async callable, invoked as `handler(request)`.
        title: An optional human-facing display name.
        mime_type: The media type reads are served as, or None to infer it from
            what the reader returned.
        requirement: What a caller must satisfy to read it -- the same
            `AuthRequirement` a tool or a route carries, decided by the same
            authorizer.
    """

    uri: str
    name: str
    description: str
    handler: Callable[..., Any]
    title: str | None = None
    mime_type: str | None = None
    requirement: AuthRequirement = NO_REQUIREMENT

    def manifest(self) -> dict[str, Any]:
        """The `resources/list` entry for this resource."""
        entry: dict[str, Any] = {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
        }
        if self.title is not None:
            entry["title"] = self.title
        if self.mime_type is not None:
            entry["mimeType"] = self.mime_type
        return entry

    def render(self, value: Any) -> dict[str, Any]:
        """One `contents` entry for whatever the reader returned.

        `bytes` travel as a base64 `blob` and everything else as `text`, because
        those are the two shapes the specification defines and a client chooses
        between them by which key is present, not by the media type.
        """
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {
                "uri": self.uri,
                "mimeType": self.mime_type or BINARY_MEDIA_TYPE,
                "blob": base64.b64encode(bytes(value)).decode("ascii"),
            }
        if isinstance(value, str):
            return {
                "uri": self.uri,
                "mimeType": self.mime_type or TEXT_MEDIA_TYPE,
                "text": value,
            }
        return {
            "uri": self.uri,
            "mimeType": self.mime_type or JSON_MEDIA_TYPE,
            "text": _json_dumps(value).decode("utf-8"),
        }


class ResourceRegistry(Catalog):
    """The declared resources of one `MCP`, plus the cached listing bytes."""

    __slots__ = ()

    noun = "resource"
    ceiling = "max_resources"
    listing_key = "resources"

    def __init__(self, *, max_resources: int = 256) -> None:
        super().__init__(max_resources)

    @property
    def resources(self) -> dict[str, Resource]:
        return self.entries

    def add(self, resource: Resource) -> None:
        """Register `resource`, refusing a duplicate URI or a full registry.

        Raises:
            ValueError: That URI is already declared, or the server is at its
                `MCPLimits.max_resources` ceiling.
        """
        self.insert(resource.uri, resource)


def build_resource(
    handler: Callable[..., Any],
    *,
    uri: str,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    mime_type: str | None = None,
    action: str | None = None,
) -> Resource:
    """Compile one reader into a `Resource`.

    Raises:
        TypeError: The handler is not an async function, or takes arguments a
            read cannot supply.
        ValueError: The URI is empty, or no description was given and the
            handler has no docstring.
    """
    if not uri:
        raise ValueError(
            "a resource needs a URI: it is how a client reads it, subscribes to "
            "it, and recognises the notifications about it."
        )
    resource_name = name or getattr(handler, "__name__", "") or uri
    if not inspect.iscoroutinefunction(handler):
        raise TypeError(
            f"resource {uri!r} must be read by an async function. Wreath reads a "
            "resource the way it invokes a route handler, and a synchronous "
            "callable would block the event loop for every other session."
        )
    text = description if description is not None else inspect.getdoc(handler)
    if not text:
        raise ValueError(
            f"resource {uri!r} needs a description. Pass `description=` or give "
            "the reader a docstring: a model chooses what to read from the "
            "listing alone, and an undescribed resource is one it reads at "
            "random or not at all."
        )
    signature = inspect.signature(handler)
    if len(signature.parameters) != 1:
        # No arguments, so no schema and nothing to validate. A reader that
        # wants a parameter is asking for a templated resource, which is a
        # different declaration this stage does not have.
        raise TypeError(
            f"resource {uri!r} must be read by a callable taking only the "
            "request. A `resources/read` carries a URI and nothing else, so "
            "there is no argument object to bind from; a resource that varies "
            "by input is a tool."
        )
    return Resource(
        uri=uri,
        name=resource_name,
        description=text,
        handler=handler,
        title=title,
        mime_type=mime_type,
        # The URI is the entity the policy is written about. A resource has a
        # stable identity and a tool does not, which is why one is resolved here
        # and the other from the call's arguments.
        requirement=NO_REQUIREMENT if action is None else policy_requirement(action, uri),
    )


def read_result(resource: Resource, value: Any) -> Mapping[str, Any]:
    """The `resources/read` result for one reader's return value."""
    return {"contents": [resource.render(value)]}


__all__ = [
    "BINARY_MEDIA_TYPE",
    "JSON_MEDIA_TYPE",
    "TEXT_MEDIA_TYPE",
    "Resource",
    "ResourceRegistry",
    "build_resource",
    "read_result",
]
