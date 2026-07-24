"""Long-running asyncio loop started in the app lifespan (supervised worker)."""
import asyncio

from .messaging import announce_booking


async def trek_reconciler(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await _sweep_underway_treks()
        except Exception:
            await asyncio.sleep(5)
        await asyncio.sleep(30)


async def _sweep_underway_treks() -> None:
    from .models import Booking

    rows = await Booking.objects.filter(status="underway").all()
    for row in rows:
        await announce_booking(str(row.id), "heartbeat")
