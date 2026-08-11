"""A drain loop started once and never joined.

``asyncio.create_task`` hands back a handle nobody keeps, so if the loop raises
the buoy queue stops draining and the service goes on answering 200 to
everything else.
"""
import asyncio

_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=512)


async def _drain_buoy_queue() -> None:
    while True:
        reading = await _QUEUE.get()
        await _store(reading)


def start_watchers() -> None:
    asyncio.create_task(_drain_buoy_queue())


async def _store(reading) -> None:
    raise NotImplementedError("the sink is configured per deployment")
