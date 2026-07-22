"""Exception handling: set/call_exception_handler and unhandled task errors."""
from __future__ import annotations

import asyncio
import gc

from .support import run


def test_callback_exception_reaches_handler(loop):
    seen = []
    loop.set_exception_handler(lambda lp, ctx: seen.append(ctx.get("exception")))

    def boom():
        raise ValueError("callback")

    async def main():
        loop.call_soon(boom)
        await asyncio.sleep(0.01)
        return [type(e).__name__ for e in seen]

    assert run(loop, main()) == ["ValueError"]


def test_call_exception_handler_is_invoked_directly(loop):
    seen = []
    loop.set_exception_handler(lambda lp, ctx: seen.append(ctx["message"]))

    async def main():
        loop.call_exception_handler({"message": "manual"})
        return list(seen)

    assert run(loop, main()) == ["manual"]


def test_get_set_exception_handler_roundtrip(loop):
    def handler(lp, ctx):
        pass

    async def main():
        loop.set_exception_handler(handler)
        return loop.get_exception_handler() is handler

    assert run(loop, main()) is True


def test_unhandled_task_exception_reaches_handler(loop):
    seen = []
    loop.set_exception_handler(lambda lp, ctx: seen.append(ctx.get("exception")))

    async def boom():
        raise RuntimeError("never retrieved")

    async def main():
        t = loop.create_task(boom())
        await asyncio.sleep(0.01)
        del t
        gc.collect()
        await asyncio.sleep(0.01)

    run(loop, main())
    assert any(isinstance(e, RuntimeError) for e in seen)
