"""Identifier transforms shared by typegen planning and rendering."""

from __future__ import annotations

import re


def pascal(text: str) -> str:
    """Turn hyphen/underscore-separated words into one PascalCase name."""
    parts = [part for part in re.split(r"[_\-]", text) if part]
    if not parts:
        return text
    return "".join(part[:1].upper() + part[1:] for part in parts)


__all__ = ["pascal"]
