"""Bookings router — the core transactional surface.

Exercises: ``APIRouter(prefix=, tags=, dependencies=)``, all five method
decorators, ``status_code=``, ``response_model=``, and many ``.objects.``
query calls with ``__`` lookups inside handler bodies (the annotate-only case).
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import get_depends, pagination
from ..messaging import announce_booking
from ..models import Booking, Llama, Ranch
from ..schemas import BookingCreate

router = APIRouter(prefix="/bookings", tags=["bookings"], dependencies=get_depends())


@router.get("", response_model=list)
async def list_bookings(request: Request, page=Depends(pagination)):
    return (
        await Booking.objects.filter(status="booked")
        .offset(page["offset"])
        .limit(page["limit"])
        .all()
    )


@router.get("/{booking_id}")
async def get_booking(booking_id: str):
    booking = await Booking.objects.filter(id=booking_id).get_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="booking not found")
    return booking


@router.post("", status_code=201, response_model=None)
async def create_booking(payload: BookingCreate, request: Request):
    llama = await Llama.objects.get_or_none(id=payload.llama_id)
    if llama is None:
        raise HTTPException(status_code=422, detail="unknown llama")
    ranch = await Ranch.objects.get(id=llama.ranch.id)
    booking = await Booking.objects.create(
        ranch=ranch, llama=llama, guests=payload.guests, notes=payload.notes
    )
    await announce_booking(str(booking.id), "booked")
    return {"id": str(booking.id)}


@router.delete("/{booking_id}", status_code=204)
async def cancel_booking(booking_id: str):
    booking = await Booking.objects.get_or_none(id=booking_id)
    if booking is not None:
        await booking.delete()


@router.patch("/{booking_id}")
async def touch_booking(booking_id: str, status: str):
    count = await Booking.objects.filter(
        ranch__slug="high-mesa", id=booking_id
    ).update(status=status)
    return {"updated": count}
