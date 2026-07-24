"""Outbound GraphQL client (herd registry) with rate-limited fan-out.

Mirror of a Strawberry *server* elsewhere: here it is a `gql` *client* + `aiometer`
concurrency limiting.
"""
from __future__ import annotations

import aiometer
from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport


def _client() -> Client:
    transport = AIOHTTPTransport(url="https://herd-registry.summit.example/graphql")
    return Client(transport=transport, fetch_schema_from_transport=False)


_LLAMA_QUERY = gql(
    """
    query Llama($id: ID!) {
      llama(id: $id) { id name altitudeToleranceM }
    }
    """
)


async def fetch_llama(llama_id: str) -> dict:
    async with _client() as session:
        return await session.execute(_LLAMA_QUERY, variable_values={"id": llama_id})


async def fetch_many(ids: list[str]) -> list[dict]:
    return await aiometer.run_all(
        [lambda i=i: fetch_llama(i) for i in ids],
        max_at_once=5,
        max_per_second=10,
    )
