"""Declared prompts: text a user chooses, not text a model invents.

A prompt is a template your application owns and a *user* invokes -- a slash
command, a menu entry -- which is the part that is easy to miss when reading the
specification next to `tools/list`. A tool is chosen by the model; a prompt is
chosen by the person. That difference is why a prompt has no `inputSchema` and
why its arguments are a flat map of strings: they are filled in by a form, not
by inference.

Wreath derives those arguments from the handler's signature, through the same
binding layer that derives a tool's schema, and **refuses a parameter that is
not a string** at registration. The specification carries prompt arguments as
`{[key: string]: string}`, so an `int` parameter is a declaration a compliant
client cannot satisfy: it would send `"3"`, validation would reject it, and the
failure would arrive at whoever was clicking the menu entry rather than at
whoever wrote the annotation.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .._auth.requirements import AuthRequirement
from ..binding import BindingSpec
from .catalog import Catalog
from .completion import completion_values
from .registry import NO_REQUIREMENT, bind_arguments, policy_requirement
from .schema import ToolSignatureError, derive_input_schema

#: The role a bare string result is attributed to. `user`, not `assistant`: a
#: prompt is what the person is about to say.
DEFAULT_ROLE = "user"


def _is_string_schema(schema: Mapping[str, Any]) -> bool:
    """Whether a rendered JSON Schema accepts strings and nothing else.

    `str | None` renders as a union rather than a plain type, and it is a
    perfectly ordinary optional argument, so the check is over every branch
    rather than over one `type` key.

    A `Literal[...]` or an `Enum` renders as a bare `enum` with no `type` at
    all, and one whose members are all strings is a string argument in every
    sense that matters here -- it is also the one shape `completion/complete`
    has anything to say about, which is why it is admitted rather than refused.
    """
    values = schema.get("enum")
    if isinstance(values, list):
        return bool(values) and all(value is None or isinstance(value, str) for value in values)
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind in ("string", "null")
    if isinstance(kind, list):
        return all(entry in ("string", "null") for entry in kind)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            return all(
                isinstance(branch, Mapping) and _is_string_schema(branch) for branch in branches
            )
    return False


@dataclass(frozen=True, slots=True)
class Prompt:
    """One declared prompt: what `prompts/list` renders and `prompts/get` fills.

    Attributes:
        name: The name a client offers to the person choosing it.
        description: What this prompt is for. Never empty.
        handler: The async callable, invoked as `handler(request, **arguments)`.
        arguments: The `prompts/list` argument descriptors, derived once.
        title: An optional human-facing display name.
        binding_spec: The compiled signature, or None for a prompt that takes
            only the request.
        requirement: What a caller must satisfy to render it.
        completions: Argument name -> the values `completion/complete` offers
            for it, read off the rendered schema of an argument annotated as a
            `Literal` or an `Enum`. Derived rather than declared: a second
            place to write the candidate values down is a second place for them
            to go stale.
    """

    name: str
    description: str
    handler: Callable[..., Any]
    arguments: tuple[dict[str, Any], ...] = ()
    title: str | None = None
    binding_spec: BindingSpec | None = None
    requirement: AuthRequirement = NO_REQUIREMENT
    completions: Mapping[str, tuple[str, ...]] = MappingProxyType({})

    def manifest(self) -> dict[str, Any]:
        """The `prompts/list` entry for this prompt."""
        entry: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.title is not None:
            entry["title"] = self.title
        if self.arguments:
            entry["arguments"] = [dict(argument) for argument in self.arguments]
        return entry

    def bind(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Validate `arguments` and return the handler's kwargs.

        Raises:
            ValidationError: One or more arguments are missing, unknown, or of
                the wrong shape.
        """
        return bind_arguments(self.binding_spec, arguments)


class PromptRegistry(Catalog):
    """The declared prompts of one `MCP`, plus the cached listing bytes."""

    __slots__ = ()

    noun = "prompt"
    ceiling = "max_prompts"
    listing_key = "prompts"

    def __init__(self, *, max_prompts: int = 128) -> None:
        super().__init__(max_prompts)

    @property
    def prompts(self) -> dict[str, Prompt]:
        return self.entries

    def add(self, prompt: Prompt) -> None:
        """Register `prompt`, refusing a duplicate name or a full registry.

        Raises:
            ValueError: That name is taken, or the server is at its
                `MCPLimits.max_prompts` ceiling.
        """
        self.insert(prompt.name, prompt)


def build_prompt(
    handler: Callable[..., Any],
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    action: str | None = None,
) -> Prompt:
    """Compile one handler into a `Prompt`, deriving its argument list.

    Raises:
        TypeError: The handler is not an async function.
        ValueError: No description was given and the handler has no docstring.
        ToolSignatureError: A parameter binds from a source a `prompts/get` has
            no way to fill, or is annotated as something other than a string.
    """
    prompt_name = name or getattr(handler, "__name__", "")
    if not prompt_name:
        raise ValueError(
            "a prompt declared from a callable without a `__name__` must be "
            "given one: `mcp.prompt(handler, name=...)`."
        )
    if not inspect.iscoroutinefunction(handler):
        raise TypeError(
            f"prompt {prompt_name!r} must be an async function, for the same "
            "reason a tool must: a synchronous callable would block the event "
            "loop for every other session."
        )
    text = description if description is not None else inspect.getdoc(handler)
    if not text:
        raise ValueError(
            f"prompt {prompt_name!r} needs a description. Pass `description=` "
            "or give the handler a docstring: it is what a person reads when "
            "choosing between prompts."
        )
    schema, spec = derive_input_schema(handler, prompt_name)
    if spec is not None and spec.body is not None:
        raise ToolSignatureError(
            f"prompt {prompt_name!r} cannot take {spec.body[0]!r} as a "
            "structured `Body()` argument. A `prompts/get` carries a flat map "
            "of strings, so a prompt takes plain string parameters or none."
        )
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    arguments: list[dict[str, Any]] = []
    completions: dict[str, tuple[str, ...]] = {}
    for argument_name, rendered in properties.items():
        if not _is_string_schema(rendered):
            raise ToolSignatureError(
                f"prompt {prompt_name!r} declares {argument_name!r} as "
                f"{rendered.get('type', 'a non-string type')!r}. MCP carries "
                "prompt arguments as a map of strings, so a compliant client "
                "would send text here and validation would refuse it -- at the "
                "person filling the form in, rather than here. Annotate it "
                "`str` and convert inside the handler."
            )
        descriptor: dict[str, Any] = {"name": argument_name}
        argument_description = rendered.get("description")
        if isinstance(argument_description, str):
            descriptor["description"] = argument_description
        descriptor["required"] = argument_name in required
        arguments.append(descriptor)
        offered = completion_values(rendered)
        if offered:
            completions[argument_name] = offered
    return Prompt(
        name=prompt_name,
        description=text,
        handler=handler,
        arguments=tuple(arguments),
        title=title,
        binding_spec=spec,
        requirement=(NO_REQUIREMENT if action is None else policy_requirement(action, prompt_name)),
        completions=MappingProxyType(completions),
    )


def render_messages(prompt: Prompt, value: Any) -> dict[str, Any]:
    """The `prompts/get` result for whatever the handler returned.

    A string is the common case and becomes one `user` message. A sequence of
    `{"role": ..., "content": ...}` mappings passes through, with a bare string
    `content` promoted to a text block so that the short spelling works.

    Raises:
        TypeError: The handler returned something that is neither.
    """
    if isinstance(value, str):
        messages = [_message(DEFAULT_ROLE, value)]
    elif isinstance(value, Sequence):
        messages = [_normalize(entry) for entry in value]
    else:
        raise TypeError(
            f"prompt {prompt.name!r} returned {type(value).__name__}; a prompt "
            "returns the text of one message, or a sequence of "
            '`{"role": ..., "content": ...}` mappings.'
        )
    return {"description": prompt.description, "messages": messages}


def _normalize(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise TypeError(
            "every prompt message must be a mapping with `role` and `content`; "
            f"got {type(entry).__name__}"
        )
    role = entry.get("role", DEFAULT_ROLE)
    content = entry.get("content")
    if isinstance(content, str):
        return _message(role, content)
    if isinstance(content, Mapping):
        return {"role": role, "content": dict(content)}
    raise TypeError(
        "a prompt message's `content` must be text or a content block; got "
        f"{type(content).__name__}"
    )


def _message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": {"type": "text", "text": text}}


__all__ = [
    "DEFAULT_ROLE",
    "Prompt",
    "PromptRegistry",
    "build_prompt",
    "render_messages",
]
