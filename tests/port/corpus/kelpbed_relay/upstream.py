"""The tide window this service annotates its own readings with.

One client per call, built where it is used, with the timeout and retry policy
spelled out at every site that needs one.
"""
import httpx

TIDE_ENDPOINT = "https://tides.kelpbed.invalid/v2"


async def fetch_tide_window(station: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{TIDE_ENDPOINT}/stations/{station}/window")
        response.raise_for_status()
        return response.json()


async def push_survey(payload: dict) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(f"{TIDE_ENDPOINT}/surveys", json=payload)
