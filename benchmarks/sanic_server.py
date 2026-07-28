"""Run the Sanic benchmark app on Sanic's native server."""

from __future__ import annotations

import argparse
import importlib
import os
from typing import Any, cast


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--app",
        default="benchmarks.apps",
        help="module exposing the Sanic application as `app`",
    )
    parser.add_argument(
        "--protocol", nargs="+", default=["http/1.1"],
        help="protocol set to serve; h3 requires --tls-cert/--tls-key and aioquic",
    )
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument(
        "--workers", type=int, default=1,
        help="server processes; the matrix gives every arm the same count",
    )
    args = parser.parse_args()
    os.environ["WREATH_BENCH_FRAMEWORK"] = "sanic"
    app = importlib.import_module(args.app).app

    options: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "access_log": False,
        "motd": False,
    }
    if args.workers > 1:
        # `single_process` and `workers` are mutually exclusive in Sanic: the
        # first bypasses its process manager entirely, the second is the manager.
        options["workers"] = args.workers
    else:
        options["single_process"] = True
    if "h2" in args.protocol:
        # Sanic's own HTTP enum is VERSION_1 and VERSION_3: it never
        # implemented HTTP/2. Refuse rather than start an HTTP/1.1 server that
        # the caller will record as h2.
        raise SystemExit("sanic does not implement HTTP/2; it serves HTTP/1 and HTTP/3")
    if args.tls_cert and args.tls_key:
        options["ssl"] = {"cert": args.tls_cert, "key": args.tls_key}
    if "h3" in args.protocol:
        if not (args.tls_cert and args.tls_key):
            raise SystemExit("sanic HTTP/3 requires --tls-cert/--tls-key")
        try:
            import aioquic  # noqa: F401
        except ImportError:
            # Without aioquic Sanic falls back to HTTP/1.1 silently, and the
            # run would be recorded as h3.
            raise SystemExit(
                "sanic HTTP/3 needs aioquic; it is in the benchmark group"
            ) from None
        options["version"] = 3

    cast(Any, app).run(**options)


if __name__ == "__main__":
    main()
