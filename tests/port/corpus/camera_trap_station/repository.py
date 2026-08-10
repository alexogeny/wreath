"""Manager injection, explicit reads, broad loads, and atomic creation."""

from .models import Camera, Sighting, StationReading, SurveyRole


class CameraRepository:
    def __init__(self, camera_orm=None):
        self.camera_orm = camera_orm or Camera.objects

    async def by_serial(self, serial):
        return await Camera.objects.get_or_none(serial=serial)


async def readings():
    return await StationReading.objects.select_all().all()


async def sightings():
    rows = await Sighting.objects.select_all().all()
    return [(row.id, row.species) for row in rows]


async def latest_reading():
    return await (
        StationReading.objects.filter(temperature__gte=0)
        .order_by("-id")
        .first()
    )


async def role(name, description):
    return await SurveyRole.objects.get_or_create(
        name=name,
        description=description,
    )
