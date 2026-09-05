from __future__ import annotations

import asyncio

_state: dict = {"lock": asyncio.Lock()}


async def ensure_e2e_peer():
    if "connection" in _state:
        return _state
    async with _state["lock"]:
        if "connection" in _state:
            return _state
        from wreath import postgres
        from wreath.http_client import ClientLimits, DestinationPolicy, HTTPClient

        from .e2e_upstream import BenchPostgres, BenchUpstreamHttp

        database = BenchPostgres()
        dsn = await database.start()
        upstream = BenchUpstreamHttp()
        upstream_port = await upstream.start()
        client = HTTPClient(
            "bench-e2e",
            base_url=f"http://127.0.0.1:{upstream_port}",
            # Socket runs admit 64 concurrent requests. A smaller pool measures
            # pool waiting and reconnect churn instead of DB+HTTP composition.
            limits=ClientLimits(
                max_connections=64,
                max_keepalive_connections=64,
            ),
            destination=DestinationPolicy(allow_private=True, allow_loopback=True),
        )
        await client.start()
        connection = await postgres.connect(dsn)
        _state.update(
            database=database,
            upstream=upstream,
            client=client,
            connection=connection,
        )
        return _state
