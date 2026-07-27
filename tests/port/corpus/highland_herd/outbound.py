"""Outbound calls on a raw httpx client, and a pandas rollup.

Two different verdicts that look alike from outside: the HTTP client has a
managed wreath equivalent worth adopting, and the dataframe work is not a
framework concern at all and should be left exactly where it is.
"""
import httpx
import pandas as pd

from .models import Trek


async def fetch_weather(paddock_name: str) -> dict:
    async with httpx.AsyncClient(base_url="https://weather.invalid", timeout=10.0) as client:
        response = await client.get("/forecast", params={"place": paddock_name})
        response.raise_for_status()
        return response.json()


async def push_summary(payload: dict) -> None:
    async with httpx.AsyncClient() as client:
        await client.post("https://reports.invalid/summaries", json=payload)


async def distance_by_month(treks: list[Trek]):
    frame = pd.DataFrame(
        [{"started_at": t.started_at, "distance_km": t.distance_km} for t in treks]
    )
    if frame.empty:
        return frame
    frame["month"] = pd.to_datetime(frame["started_at"]).dt.to_period("M")
    return frame.groupby("month")["distance_km"].sum().reset_index()
