"""Web byte codecs: percent decoding, query strings, cookie headers."""

from __future__ import annotations

from ._native import _core

if _core is not None:
    percent_decode = _core.percent_decode
    parse_qs = _core.parse_qs
    parse_cookies = _core.parse_cookies
else:
    from ._pure.codecs import parse_cookies, parse_qs, percent_decode

__all__ = ["parse_cookies", "parse_qs", "percent_decode"]
