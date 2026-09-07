from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from time import process_time_ns
from types import SimpleNamespace


def module_paths(*modules):
    return [Path(module.__file__).resolve() for module in modules]


async def measure(args):
    sys.path.insert(0, str(args.source.resolve()))
    from wreath import _sigv4, objects

    modules = module_paths(_sigv4, objects)
    if any(not path.is_relative_to(args.source.resolve()) for path in modules):
        raise RuntimeError("signing benchmark loaded an unexpected source root")
    dates = ("20260905T040000Z", "20260906T040000Z")
    date = dates[0]
    objects._amz_date = lambda: date
    credentials = dict(
        region="us-east-1",
        access_key="fixture-access",
        secret_key="fixture-secret",
    )
    options = dict(
        **credentials,
        service="s3",
        method="GET",
        path="/key",
        host="fixture.s3.us-east-1.amazonaws.com",
    )
    expected = {}
    urls = {}
    for stamp in dates:
        urls[stamp] = _sigv4.presign(**options, amz_date=stamp, expires=60)
        for window in (None, 0, 1, 2, 3):
            headers = (
                {}
                if window is None
                else {"range": f"bytes={window * 65536}-{(window + 1) * 65536 - 1}"}
            )
            signed = _sigv4.sign(**options, amz_date=stamp, headers=headers)
            expected[stamp, window] = tuple(
                (key.encode("ascii"), value.encode("latin-1"))
                for key, value in {"host": options["host"], **headers, **signed}.items()
            )
    payload = b"x" * 65536
    requests = 0

    async def request(method, target, *, headers, body):
        nonlocal requests
        requests += 1
        wire = dict(headers)
        span = wire.get(b"range")
        window = None if span is None else int(span.split(b"=")[1].split(b"-")[0]) // 65536
        if method != "GET" or target != "/key" or body != b"" or headers != expected[date, window]:
            raise RuntimeError("fake-client request differs from uncached signing oracle")
        return SimpleNamespace(status=200 if span is None else 206, body=payload)

    client = SimpleNamespace(request=request)

    def make_store():
        return objects.S3ObjectStore(client, bucket="fixture", window=65536, **credentials)

    store = make_store()
    await store.read("key")
    requests = 0
    checksum = 0
    started = process_time_ns()
    for index in range(args.repeats):
        if args.scenario == "cold":
            store = make_store()
        if args.scenario == "rollover":
            date = dates[index % 2]
        if args.scenario == "windows":
            async for chunk in store.read_stream("key", range=(0, 4 * 65536 - 1)):
                checksum += len(chunk)
        elif args.scenario == "url":
            output = store.url("key", expires=60)
            if output != urls[date]:
                raise RuntimeError("presigned URL differs from uncached oracle")
            checksum += len(output)
        elif args.scenario == "standalone":
            output = _sigv4.sign(**options, amz_date=date)
            if output["Authorization"].encode() != dict(expected[date, None])[b"Authorization"]:
                raise RuntimeError("standalone signature differs from oracle")
            checksum += len(output["Authorization"])
        else:
            checksum += len(await store.read("key"))
    elapsed = process_time_ns() - started
    cache = getattr(store, "_signing_keys", None)
    cache_bytes = 0
    if cache is not None:
        scope, key = cache._entry
        cache_bytes = sum(map(sys.getsizeof, (cache, cache._entry, scope, key, scope[1])))
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "cache_owned_bytes": cache_bytes,
                "store_dict_bytes": sys.getsizeof(store.__dict__),
                "artifacts": [
                    {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in modules
                ],
            }
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "scenario": args.scenario,
                "checksum": checksum,
                "requests": requests,
                "wire_sha256": hashlib.sha256(repr((expected, urls)).encode()).hexdigest(),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2000)
    parser.add_argument(
        "--scenario",
        choices=("read", "windows", "url", "cold", "rollover", "standalone"),
        required=True,
    )
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
