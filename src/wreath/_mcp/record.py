"""One Flight Recorder marker per `tools/call`, and the arguments beside it.

A tool call is the one thing about an MCP server that an operator will
eventually have to reconstruct: which model asked for what, on whose behalf,
and what happened. That is a structured log record, and Wreath already writes
those onto the recorder's ring -- so this module declares one call site and
nothing else. There is no MCP-specific sink, no second format, and no new
redaction code.

**Redaction is borrowed twice over, deliberately.** Argument *values* go through
`wreath.logging`'s deny-by-default rule: a scalar is written, a string is
fingerprinted. Argument *names* go through `wreath.crud.SENSITIVE_FIELD`, the
same regular expression that hides a password column from a generated CRUD
endpoint and from the GraphQL schema. A name it matches is recorded as present
and never as a value -- not even as a fingerprint, because a fingerprint of a
password is an offline guessing oracle and a fingerprint of a session token is a
correlation handle.

The arguments ride the request's canonical log line rather than the marker,
because the set of them is different for every tool and a call site's field list
is fixed at declaration. A wide event is exactly where a variable set of
per-request facts belongs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import logging as _log
from ..crud import SENSITIVE_FIELD

#: The value stored in place of a sensitive argument. A constant, so writing it
#: raw discloses nothing: it says an argument was supplied under a name that
#: looks like a secret, which is the fact an audit trail needs and the only one.
REDACTED = "<redacted>"

#: Prefix for every argument field, so a tool argument named `status` cannot
#: overwrite a field the framework or the application already set.
_PREFIX = "mcp.arg."

#: Prefix for what a person typed into an `elicitation/create` form. Distinct
#: from the arguments' prefix because the two have different provenance -- one
#: is what the model asked for and the other is what a human answered -- and an
#: audit that cannot tell them apart cannot answer "who supplied this". The
#: redaction rules either side of the prefix are identical, deliberately: the
#: field most likely to arrive here is the one a form labelled "password".
ELICIT_PREFIX = "mcp.elicit."

#: How deep a structured argument is flattened onto the canonical line. One
#: level covers the shape the binding layer produces -- a dataclass or model
#: marked `Annotated[T, Body()]` -- and refuses to walk an arbitrary graph a
#: caller controls.
_MAX_DEPTH = 1

#: The `tools/call` marker's message and its declared arguments.
#:
#: `tool`, `outcome` and `principal` are RAW on purpose, against the
#: deny-by-default rule the other string fields follow. A fingerprinted tool
#: name answers "was it the same tool" and never "which tool", and this record
#: exists to answer the second; the principal is the subject the caller's own
#: token asserts, which is not a secret from the operator whose server just ran
#: something on its behalf. The session id is *not* raw: it is a bearer
#: credential for that session's in-flight calls, so it is fingerprinted, which
#: still correlates a session's calls with each other.
TOOL_CALL_TEMPLATE = (
    "MCP tool {tool} {outcome} in {duration_ms}ms for {principal} (session {session})"
)
TOOL_CALL_FIELDS = (
    _log.field("tool", str, _log.RAW),
    _log.field("outcome", str, _log.RAW),
    _log.field("duration_ms", float),
    _log.field("principal", str, _log.RAW),
    _log.field("session", str, _log.HASHED),
)

#: The site, and the registry it belongs to. A logging runtime is replaceable --
#: the server installs a configured one at startup, and a test installs its own
#: -- and a site interned at import belongs to whichever registry was installed
#: *then*. Records carrying a stale id read back as another site's, so the site
#: is re-interned when the registry moves. Interning is keyed on the template
#: text and therefore idempotent, which is what makes re-entering an earlier
#: runtime safe rather than a duplicate registration. Steady state is one
#: identity comparison, the same trade `_auth.permissions._vocabulary_reader`
#: makes against the route table.
_MARKER: tuple[object, _log.LogEvent] | None = None


def marker() -> _log.LogEvent:
    """The `tools/call` call site, interned in the installed runtime."""
    global _MARKER
    registry = _log.installed().registry
    cached = _MARKER
    if cached is None or cached[0] is not registry:
        cached = (
            registry,
            _log.LogEvent(
                registry.intern_template(TOOL_CALL_TEMPLATE, _log.INFO, TOOL_CALL_FIELDS)
            ),
        )
        _MARKER = cached
    return cached[1]


#: Outcomes, in the vocabulary the counters use. Every `tools/call` records
#: exactly one, so "how many denials yesterday" is a filter rather than an
#: inference from what is missing.
OUTCOME_OK = "ok"
OUTCOME_TOOL_ERROR = "tool_error"
OUTCOME_RAISED = "raised"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_DENIED = "denied"
OUTCOME_THROTTLED = "throttled"
OUTCOME_REJECTED = "schema_rejected"
#: A tool asked the client's model to generate, and it did. The same marker as a
#: `tools/call` and deliberately so: a server that spends a caller's model is
#: doing something an operator has to be able to reconstruct afterwards, and
#: giving it a record of its own would have meant a second site, a second field
#: list, and a second thing to remember to redact.
OUTCOME_SAMPLED = "sampled"
#: A sampling request the tool was not allowed to make: it declared no
#: `sampling=`, its Cedar policy said no, or the client never advertised the
#: capability. A refusal, and it never reached a model.
OUTCOME_SAMPLE_DENIED = "sample_denied"
#: A sampling request refused by the tool's own rate limit -- the same bucket
#: `tools/call` spends, because a tool that samples on every call is asking for
#: the caller's model twice per invocation.
OUTCOME_SAMPLE_THROTTLED = "sample_throttled"
#: A sampling request that went out and did not come back: the client answered
#: with an error, or answered not at all.
OUTCOME_SAMPLE_FAILED = "sample_failed"
#: A tool put a form in front of the person at the other end and they filled it
#: in. Recorded on the same marker as the call, for the same reason sampling is:
#: an operator reconstructing an incident needs to know that a prompt appeared
#: in the client's own chrome, under whose session, and what came back -- and the
#: answer itself is already beside it under `ELICIT_PREFIX`.
OUTCOME_ELICITED = "elicited"
#: A form the person said no to, or closed. Not a failure at any level: this is
#: the mechanism working, and it is counted apart from the refusals so that
#: "people keep declining this tool" stays legible.
OUTCOME_ELICIT_DECLINED = "elicit_declined"
#: An elicitation the tool was not allowed to make: it declared no
#: `elicitation=`, its Cedar policy said no, or the client never advertised the
#: capability. A refusal, and **no prompt ever reached a person** -- which is the
#: whole point of the gate, because a dialog is a weak defence against a request
#: that was crafted to look legitimate.
OUTCOME_ELICIT_DENIED = "elicit_denied"
#: An elicitation refused by the tool's own rate limit -- the same bucket
#: `tools/call` spends, because a tool that can re-prompt without limit can wear
#: a person down until they answer.
OUTCOME_ELICIT_THROTTLED = "elicit_throttled"
#: An elicitation that went out and did not come back: the client answered with
#: an error, answered not at all, or answered something the schema forbids.
OUTCOME_ELICIT_FAILED = "elicit_failed"

#: Callers with no verified identity. A literal, so the field is never absent
#: and a query for "calls by nobody" does not have to test for null.
ANONYMOUS = "anonymous"


def record_call(
    *,
    tool: str,
    outcome: str,
    duration_ms: float,
    principal: str | None,
    session: str,
) -> None:
    """Emit the marker for one `tools/call`."""
    marker()(
        tool,
        outcome,
        duration_ms,
        ANONYMOUS if principal is None else principal,
        session,
    )


def record_arguments(arguments: Mapping[str, Any], *, prefix: str = _PREFIX) -> None:
    """Attach a call's arguments to the request's canonical log line.

    A no-op outside a request scope, which is what `wreath.logging.set_field`
    already promises, so a tool called from a test or a process with no logging
    runtime installed costs a dictionary walk and nothing else.

    `prefix` is the only thing an elicitation response changes. It goes through
    this function rather than beside it because the redaction that matters --
    `crud.SENSITIVE_FIELD` on the name, deny-by-default on the value -- must not
    exist twice: a second copy is a second thing to forget, and the value most
    likely to arrive from a form is a password.
    """
    for name, value in arguments.items():
        _record_one(prefix, name, value, _MAX_DEPTH)


def _record_one(prefix: str, name: str, value: Any, depth: int) -> None:
    key = prefix + name
    if SENSITIVE_FIELD.search(name):
        _log.set_field(key, REDACTED, raw=True)
        return
    if isinstance(value, Mapping):
        if depth <= 0:
            _log.set_field(key, _shape(value), raw=True)
            return
        for inner, nested in value.items():
            _record_one(key + ".", str(inner), nested, depth - 1)
        return
    if value is None or isinstance(value, (bool, int, float, str)):
        # Deny-by-default from here: `logging` writes the scalar and
        # fingerprints the string. Nothing about that decision is made here.
        _log.set_field(key, value)
        return
    _log.set_field(key, _shape(value), raw=True)


def _shape(value: Any) -> str:
    """A value's *type*, for one this cannot record. Never the value itself."""
    return f"<{type(value).__name__}>"


__all__ = [
    "ANONYMOUS",
    "ELICIT_PREFIX",
    "OUTCOME_CANCELLED",
    "OUTCOME_DENIED",
    "OUTCOME_ELICITED",
    "OUTCOME_ELICIT_DECLINED",
    "OUTCOME_ELICIT_DENIED",
    "OUTCOME_ELICIT_FAILED",
    "OUTCOME_ELICIT_THROTTLED",
    "OUTCOME_OK",
    "OUTCOME_RAISED",
    "OUTCOME_REJECTED",
    "OUTCOME_SAMPLED",
    "OUTCOME_SAMPLE_DENIED",
    "OUTCOME_SAMPLE_FAILED",
    "OUTCOME_SAMPLE_THROTTLED",
    "OUTCOME_THROTTLED",
    "OUTCOME_TOOL_ERROR",
    "REDACTED",
    "TOOL_CALL_FIELDS",
    "TOOL_CALL_TEMPLATE",
    "marker",
    "record_arguments",
    "record_call",
]
