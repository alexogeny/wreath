"""Run the Wreath benchmark application on Wreath's native HTTP server."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os

from wreath.server import ServerConfig, TLSConfig, serve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--app",
        default="benchmarks.apps:app",
        help="module:attribute of the ASGI application to serve",
    )
    parser.add_argument("--loop", choices=("asyncio", "uvloop"), default="asyncio")
    parser.add_argument(
        "--protocol", nargs="+", default=["http/1.1"],
        choices=("http/1.1", "h2", "h3"),
        help="protocol set to serve (h2/h3 require --tls-cert/--tls-key)",
    )
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    args = parser.parse_args()
    module_name, _, attribute = args.app.partition(":")
    app = getattr(importlib.import_module(module_name), attribute or "app")
    timers_disabled = os.environ.get("WREATH_BENCH_DISABLE_TIMERS") == "1"
    protocols = tuple(dict.fromkeys(args.protocol))
    config = ServerConfig(
        host=args.host,
        port=args.port,
        lifespan="off",
        protocols=protocols,  # type: ignore[arg-type]
        keep_alive_timeout=0.0 if timers_disabled else 5.0,
        request_timeout=0.0 if timers_disabled else 30.0,
    )
    tls = None
    if args.tls_cert and args.tls_key:
        tls = TLSConfig(certfile=args.tls_cert, keyfile=args.tls_key)

    async def run_server() -> None:
        server = await serve(app, config, tls=tls)
        await server.serve_forever()

    if args.loop == "uvloop":
        import uvloop

        uvloop.run(run_server())
    else:
        asyncio.run(run_server())


if __name__ == "__main__":
    main()
