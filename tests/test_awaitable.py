from __future__ import annotations

import types
from collections.abc import Awaitable, Iterator

from wreath._awaitable import is_awaitable


class _CustomAwaitable:
    def __await__(self) -> Iterator[None]:
        if False:
            yield None
        return None


@types.coroutine
def _iterable_coroutine() -> Iterator[None]:
    if False:
        yield None


def test_python_awaitable_shapes_are_recognized() -> None:
    async def native() -> None:
        return None

    coroutine = native()
    generator = _iterable_coroutine()
    try:
        assert is_awaitable(coroutine)
        assert is_awaitable(_CustomAwaitable())
        assert is_awaitable(generator)
        assert not is_awaitable(object())
    finally:
        coroutine.close()
        generator.close()


def test_awaitable_abc_registration_does_not_claim_an_unawaitable_object() -> None:
    class RegisteredOnly:
        pass

    Awaitable.register(RegisteredOnly)
    value = RegisteredOnly()

    assert isinstance(value, Awaitable)
    assert not is_awaitable(value)
