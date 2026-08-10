"""Process-lifetime polling and request-bounded fan-out."""

import asyncio


async def watch_cameras(stop):
    while not stop.is_set():
        await poll_camera_network()


async def start_watch(stop):
    asyncio.create_task(watch_cameras(stop))


async def load_thumbnails(sightings):
    tasks = [asyncio.create_task(load_thumbnail(row)) for row in sightings]
    return await asyncio.gather(*tasks)


async def load_camera_batch(cameras):
    tasks = []
    for camera in cameras:
        task = asyncio.create_task(load_camera(camera))
        tasks.append(task)
    return await asyncio.gather(*tasks)
