"""Argument autocompletion, from declarations that already exist.

`completion/complete` is where an MCP server tells a client what a prompt's
argument may be, so the person choosing a slash command gets a menu instead of a
free-text box. The tempting implementation is a second registry -- decorate a
completer per argument, keep it beside the prompt, remember to update it. That
registry is a copy of something already written down, and a copy drifts.

Wreath has no completion declaration, deliberately. A prompt argument annotated
`Literal["ridge", "creek"]` or with an `Enum` already renders as an `enum` in the
schema `derive_input_schema` produces, and that list *is* the completion. So the
values come off the rendered schema at registration time, filtered per request
by whatever the client has typed so far. Annotate the argument and the menu
appears; there is nothing else to declare and nothing to keep in step.

Resource completion answers with nothing, and that is not a stub: completions on
a resource reference complete a **templated** URI's placeholder, Wreath declares
no templated resources, and `resources/templates/list` already says so by
returning an empty list. An empty completion is the same statement in the same
vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .protocol import INVALID_PARAMS, JsonRpcError

#: The most values one answer carries. The specification caps a completion at
#: 100 and asks the server to say whether it truncated, which is what `hasMore`
#: and `total` are for.
MAX_VALUES = 100


def completion_values(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Every string an argument's rendered schema says it may be.

    Read off the schema rather than off the annotation, so an `Enum`, a
    `Literal`, and an optional either of them all arrive here as one shape.
    """
    values = schema.get("enum")
    if isinstance(values, list):
        return tuple(value for value in values if isinstance(value, str))
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            found: list[str] = []
            for branch in branches:
                if isinstance(branch, Mapping):
                    found.extend(completion_values(branch))
            return tuple(found)
    return ()


def complete(prompts: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Answer one `completion/complete`.

    Raises:
        JsonRpcError: The reference or the argument is malformed, or names a
            prompt this server does not declare.
    """
    reference = params.get("ref")
    if not isinstance(reference, Mapping):
        raise JsonRpcError(INVALID_PARAMS, "`params.ref` must be a reference object")
    argument = params.get("argument")
    if not isinstance(argument, Mapping):
        raise JsonRpcError(
            INVALID_PARAMS, "`params.argument` must carry the argument's name and value"
        )
    name = argument.get("name")
    typed = argument.get("value")
    if not isinstance(name, str):
        raise JsonRpcError(INVALID_PARAMS, "`params.argument.name` must be a string")
    if typed is not None and not isinstance(typed, str):
        raise JsonRpcError(INVALID_PARAMS, "`params.argument.value` must be a string")

    kind = reference.get("type")
    if kind == "ref/resource":
        # Nothing to complete, because a resource completion completes a
        # template's placeholder and this server declares no templates.
        return _answer(())
    if kind != "ref/prompt":
        raise JsonRpcError(
            INVALID_PARAMS,
            f"unknown completion reference type {kind!r}; this revision defines "
            "`ref/prompt` and `ref/resource`",
        )

    prompt_name = reference.get("name")
    if not isinstance(prompt_name, str):
        raise JsonRpcError(INVALID_PARAMS, "`params.ref.name` must name a prompt")
    prompt = prompts.get(prompt_name)
    if prompt is None:
        raise JsonRpcError(INVALID_PARAMS, f"unknown prompt {prompt_name!r}")
    candidates = prompt.completions.get(name, ())
    prefix = (typed or "").lower()
    return _answer(tuple(value for value in candidates if value.lower().startswith(prefix)))


def _answer(values: tuple[str, ...]) -> dict[str, Any]:
    return {
        "completion": {
            "values": list(values[:MAX_VALUES]),
            "total": len(values),
            "hasMore": len(values) > MAX_VALUES,
        }
    }


__all__ = ["MAX_VALUES", "complete", "completion_values"]
