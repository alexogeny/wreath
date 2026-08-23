"""Cheap, exact recognition of objects Python can actually await."""

from __future__ import annotations

import inspect
import types
from typing import Any

_GENERATOR_TYPE = types.GeneratorType
_ITERABLE_COROUTINE = inspect.CO_ITERABLE_COROUTINE


def is_awaitable(value: Any) -> bool:
    """Recognize native, custom, and iterable-generator awaitables.

    ``inspect.isawaitable`` ends with an abstract-base-class membership test.
    Looking for ``__await__`` on the type asks the same question Python's
    ``await`` expression asks, without paying for that ABC lookup.  A generator
    decorated with ``types.coroutine`` is the sole shape whose awaitability is
    carried on its code flags instead of its type.
    """
    cls = value.__class__
    if hasattr(cls, "__await__"):
        return True
    if cls is _GENERATOR_TYPE:
        return bool(value.gi_code.co_flags & _ITERABLE_COROUTINE)
    return False
