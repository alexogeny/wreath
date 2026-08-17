"""Shaping and translating validation errors.

Wreath already answers a failed body/query/path validation with RFC 9457, which
is the right envelope. What applications add on top is presentation: their own
field names, their own wording, and their own language:

```python
from wreath.validation_errors import MessageCatalogue, catalogue_formatter

catalogue = MessageCatalogue({
    "en": {"missing": "This field is required.", "int": "Enter a whole number."},
    "fr": {"missing": "Ce champ est obligatoire.", "int": "Entrez un entier."},
})
app.set_validation_formatter(catalogue_formatter(catalogue))
```
The default output is unchanged from what Wreath has always produced, so
installing nothing keeps today's behaviour byte for byte.

An error carries a machine-readable `type` (`"missing"`, `"int"`,
`"too_complex"`, ...) produced by both the Python validator and
`_native/validate.c`. That is the catalogue key -- translate on `type`, never
on the English `msg`, which is a developer-facing default and may change.

Formatting remains Python because it happens only on a 422. Language
negotiation is the reusable exception: one native selector owns the q-value
scan and returns the chosen offered object, so Python is materialized only at
the header and catalogue boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ._native import _core
from .response import ProblemDetail

if TYPE_CHECKING:
    from .request import Request

__all__ = [
    "MessageCatalogue",
    "ValidationFormatter",
    "catalogue_formatter",
    "default_validation_problem",
    "select_language",
]

#: `(errors, request) -> ProblemDetail`. `errors` is the raw list of
#: `{"loc": [...], "msg": str, "type": str}` dicts.
ValidationFormatter = Callable[[list[dict[str, Any]], "Request"], ProblemDetail]


def default_validation_problem(
    errors: list[dict[str, Any]], request: Request
) -> ProblemDetail:
    """Exactly what Wreath produced before formatters existed."""
    return ProblemDetail(
        status=422,
        detail="Request validation failed",
        extensions={"errors": errors},
    )


# --- language negotiation ----------------------------------------------------


def select_language(accept_language: str | bytes | None, offered: Sequence[str]) -> str:
    """Pick the best of `offered` for an `Accept-Language` header.

    `offered` is in preference order and must not be empty; its first entry is
    the fallback. Matching is case-insensitive and honours prefixes, so a client
    asking for `en-GB` is served `en` when only `en` is offered. `q=0`
    explicitly refuses a language, and `*` accepts anything still on offer.

    Returns the fallback for a missing, empty, or unparseable header -- a bad
    Accept-Language is never a client error.
    """
    if not offered:
        raise ValueError("offered must contain at least one language")
    fallback = offered[0]
    if not accept_language:
        return fallback
    if isinstance(accept_language, bytes):
        try:
            accept_language = accept_language.decode("latin-1")
        except UnicodeDecodeError:
            return fallback

    return _core.select_language(accept_language, offered)


# --- catalogue ---------------------------------------------------------------


class MessageCatalogue:
    """Per-language messages keyed by an error's `type`.

    The first language given is the default and the negotiation fallback. A
    language missing a key falls back to the default language's message, and a
    key missing everywhere leaves the validator's own `msg` in place -- a
    partial translation degrades to English rather than to a blank.
    """

    __slots__ = ("_default", "_languages", "_messages")

    def __init__(self, messages: Mapping[str, Mapping[str, str]]) -> None:
        if not messages:
            raise ValueError("a catalogue needs at least one language")
        self._messages = {
            language: dict(entries) for language, entries in messages.items()
        }
        self._languages = list(self._messages)
        self._default = self._languages[0]

    @property
    def languages(self) -> list[str]:
        return list(self._languages)

    @property
    def default_language(self) -> str:
        return self._default

    def message(self, language: str, kind: str, fallback: str) -> str:
        entries = self._messages.get(language)
        if entries is not None:
            found = entries.get(kind)
            if found is not None:
                return found
        found = self._messages[self._default].get(kind)
        return found if found is not None else fallback

    def for_request(self, accept_language: str | bytes | None) -> str:
        return select_language(accept_language, self._languages)


def catalogue_formatter(
    catalogue: MessageCatalogue,
    *,
    aliases: Mapping[str, str] | None = None,
    detail: str = "Request validation failed",
) -> ValidationFormatter:
    """A formatter that translates each error's `msg` via `catalogue`.

    `aliases` renames the *last* path segment of a location, so an external
    API can expose `userName` for a Python `user_name` without the handler
    signature changing.

    The emitted problem carries the negotiated language in a `language`
    extension, so a client can tell what it was served.
    """
    alias_map = dict(aliases or {})

    def format_errors(
        errors: list[dict[str, Any]], request: Request
    ) -> ProblemDetail:
        language = catalogue.for_request(request.header("accept-language"))
        shaped: list[dict[str, Any]] = []
        for error in errors:
            kind = str(error.get("type", ""))
            location = list(error.get("loc", ()))
            if location and isinstance(location[-1], str):
                location[-1] = alias_map.get(location[-1], location[-1])
            shaped.append({
                "loc": location,
                "msg": catalogue.message(language, kind, str(error.get("msg", ""))),
                "type": kind,
            })
        return ProblemDetail(
            status=422,
            detail=detail,
            extensions={"errors": shaped, "language": language},
        )

    return format_errors


def field_names(errors: Iterable[Mapping[str, Any]]) -> list[str]:
    """The field name each error points at, for a flat form-style rendering."""
    names: list[str] = []
    for error in errors:
        location = list(error.get("loc", ()))
        names.append(str(location[-1]) if location else "")
    return names
