"""RabbitMQ-style publish via the fictional herd-radio broker (private package)."""
from tumbleweed_core import HerdRadio

_radio = HerdRadio()


async def announce_booking(booking_id: str, status: str) -> None:
    await _radio.publish(
        "bookings", routing_key=f"booking.{status}", body={"id": booking_id}
    )
