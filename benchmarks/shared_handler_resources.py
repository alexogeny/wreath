import argparse
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns


def make_endpoint(kind):
    if kind == "request-only":

        async def endpoint(request) -> str:
            return "ok"
    elif kind == "bound":

        async def endpoint(request, id: int) -> str:
            return str(id)
    else:

        async def endpoint() -> str:
            return "ok"

    return endpoint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--routes", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--kind",
        choices=("shared", "request-only", "unique", "bound", "interleaved"),
        required=True,
    )
    parser.add_argument("--phase", choices=("facts", "openapi"), required=True)
    args = parser.parse_args()
    if args.routes < 1 or args.repeats < 1:
        parser.error("routes and repeats must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath import Wreath
    from wreath import app as app_module
    from wreath.openapi import generate_openapi

    source = Path(app_module.__file__).resolve()
    if not source.is_relative_to(args.source.resolve()):
        raise RuntimeError("shared handler benchmark loaded wrong source")
    paths = [
        f"/route/{{id}}/{index}" if args.kind == "bound" else f"/route/{index}"
        for index in range(args.routes)
    ]

    def prepare():
        app = Wreath(ai_scraping="allow", hardening="off")
        shared = make_endpoint(args.kind)
        alternate = make_endpoint(args.kind)
        for index, path in enumerate(paths):
            if args.kind == "unique":
                handler = make_endpoint(args.kind)
            elif args.kind == "interleaved" and index % 2:
                handler = alternate
            else:
                handler = shared
            app.get(path)(handler)
        return app

    def run(app):
        if args.phase == "facts":
            image = app._application_image
            return image.binding_specs(), image.return_annotations(), image.requestless()
        return generate_openapi(app)

    def validate(result):
        if args.phase == "facts":
            specs, annotations, requestless = result
            if annotations != (str,) * args.routes:
                raise RuntimeError("incorrect return annotations")
            expected_requestless = args.kind not in ("request-only", "bound")
            if requestless != (expected_requestless,) * args.routes:
                raise RuntimeError("incorrect requestless facts")
            if args.kind == "bound":
                if len(specs) != args.routes or any(
                    spec.path_params != (("id", "id", int),) for spec in specs
                ):
                    raise RuntimeError("route-bound parameters were not analyzed")
            elif specs != (None,) * args.routes:
                raise RuntimeError("incorrect unbound facts")
            return {"routes": len(specs), "requestless": expected_requestless, "returns": "str"}
        if set(result["paths"]) != set(paths):
            raise RuntimeError("OpenAPI path set differs from declarations")
        for path in paths:
            operation = result["paths"][path]["get"]
            if not operation["operationId"] or "200" not in operation["responses"]:
                raise RuntimeError("OpenAPI operation is incomplete")
        return result

    elapsed = 0
    for _ in range(args.repeats):
        app = prepare()
        started = process_time_ns()
        result = run(app)
        elapsed += process_time_ns() - started
        validate(result)
        del result, app
    app = prepare()
    tracemalloc.start()
    try:
        result = run(app)
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    output = validate(result)
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "peak_bytes": peak,
                "retained_bytes": retained,
                "source_path": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "kind": args.kind,
                "phase": args.phase,
                "sha256": hashlib.sha256(json.dumps(output, sort_keys=True).encode()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
