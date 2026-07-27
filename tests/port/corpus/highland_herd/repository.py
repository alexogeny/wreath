"""The ``.objects.`` surface, at the ratio a real ormar codebase has it.

In the tree this was modelled on, ``filter`` outnumbers everything else about
four to one, ``get_or_none`` and ``create`` are next, and the eager-load verbs
are rare but load-bearing. Each verb becomes a different wreath call, which is
the whole reason the codemod classifies them rather than reporting one number.
"""
from .models import Llama, Paddock, Rider, Trek


async def llamas_in_paddock(paddock_id) -> list[Llama]:
    return await Llama.objects.filter(paddock=paddock_id, retired=False).all()


async def llamas_above_grade(grade: int) -> list[Llama]:
    return await Llama.objects.filter(grade__gte=grade).order_by("name").all()


async def search_llamas(term: str) -> list[Llama]:
    return await Llama.objects.filter(name__icontains=term).limit(50).all()


async def find_llama(llama_id) -> Llama | None:
    return await Llama.objects.get_or_none(id=llama_id)


async def find_rider(handle: str) -> Rider | None:
    return await Rider.objects.get_or_none(handle=handle)


async def require_paddock(paddock_id) -> Paddock:
    # Raises NoMatch when absent — the miss branch a port has to preserve.
    return await Paddock.objects.get(id=paddock_id)


async def record_trek(llama_id, rider_id, distance_km: float) -> Trek:
    return await Trek.objects.create(
        llama=llama_id, rider=rider_id, distance_km=distance_km
    )


async def enrol_llama(name: str, paddock_id) -> Llama:
    return await Llama.objects.create(name=name, paddock=paddock_id)


async def every_paddock() -> list[Paddock]:
    return await Paddock.objects.all()


async def treks_with_llamas() -> list[Trek]:
    # The eager load. Wreath makes this mandatory rather than optional.
    return await Trek.objects.select_related("llama").all()


async def llama_with_treks(llama_id) -> Llama | None:
    return await Llama.objects.select_all().get_or_none(id=llama_id)


async def llama_names() -> list[dict]:
    return await Llama.objects.values(["id", "name"])


async def import_llamas(rows: list[dict]) -> None:
    await Llama.objects.bulk_create([Llama(**row) for row in rows])


async def count_active() -> int:
    return await Llama.objects.filter(retired=False).count()


async def paddock_is_used(paddock_id) -> bool:
    return await Llama.objects.filter(paddock=paddock_id).exists()


async def purge_retired() -> None:
    await Llama.objects.filter(retired=True).delete()


async def newest_trek() -> Trek | None:
    return await Trek.objects.order_by("-started_at").first()


async def ensure_paddock(name: str) -> Paddock:
    paddock, _created = await Paddock.objects.get_or_create(name=name)
    return paddock
