"""A short-lived HTTP client used to call the weather station."""

import httpx


async def forecast(station_url, token):
    async with httpx.AsyncClient(
        base_url=station_url,
        headers={"x-station-token": token},
        timeout=10.0,
    ) as client:
        response = await client.get("/forecast")
        response.raise_for_status()
        return response.json()
