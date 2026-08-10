"""Web byte codecs: percent decoding, query strings, cookie headers.

**The annotations here are load-bearing, not decoration.** A compiled function
is `Any` to a type checker, so a facade that merely rebinds `_core.parse_qs`
tells a caller nothing -- and that shows up as a *downstream* error somewhere
that then infers `dict[Any, None | Any]` for a dict it should know is
`dict[str, str]`. This facade is where the C's signature is written down.
"""

from __future__ import annotations

from collections.abc import Callable

from ._native import _core

#: `percent_decode(data, plus_as_space=False)` -- `%xx` unescaping, with the
#: form-encoding variant that also reads `+` as a space.
percent_decode: Callable[..., bytes] = _core.percent_decode

#: `parse_qs(query, max_fields=0)` -- form encoding to ordered pairs, raising
#: `ValueError` past `max_fields` and for nothing else.
parse_qs: Callable[..., list[tuple[str, str]]] = _core.parse_qs

#: `parse_cookies(header)` -- a request `Cookie` header to a mapping, first
#: occurrence of a repeated name winning, whitespace stripped from both halves.
parse_cookies: Callable[[bytes], dict[str, str]] = _core.parse_cookies

__all__ = ["parse_cookies", "parse_qs", "percent_decode"]
