from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import sys
import time
import tracemalloc
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure neutral authorization merge storage.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--application", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    module = importlib.import_module("wreath._auth.requirements")
    if module.__file__ is None or not Path(module.__file__).is_relative_to(source):
        raise RuntimeError("loaded requirements outside selected source")
    metrics = {}
    outputs = []
    if args.application:
        wreath = importlib.import_module("wreath")
        openapi = importlib.import_module("wreath.openapi")

        def endpoint_factory():
            async def endpoint() -> str:
                return "ok"

            return endpoint

        def make_application():
            application = wreath.Wreath(ai_scraping="allow")
            for index in range(8000):
                application.get(f"/items/{index}")(endpoint_factory())
            return application

        expected_paths = {f"/items/{index}" for index in range(8000)}

        def verify(application, document):
            if set(document["paths"]) != expected_paths or document["openapi"] != "3.1.0":
                raise RuntimeError("OpenAPI paths or version differ")
            requirements = application._application_image._requirements
            if len(requirements) != 8000:
                raise RuntimeError("not all route requirements were retained")
            if any(value != module.AuthRequirement() for value in requirements):
                raise RuntimeError("default route requirements differ")
            encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            return hashlib.sha256(encoded).hexdigest()

        application = make_application()
        started = time.process_time_ns()
        document = openapi.generate_openapi(application)
        metrics["application_cpu_ns"] = time.process_time_ns() - started
        digest = verify(application, document)
        del application, document
        gc.collect()
        application = make_application()
        tracemalloc.start()
        document = openapi.generate_openapi(application)
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        metrics["application_retained_bytes"] = retained
        metrics["application_peak_bytes"] = peak
        if verify(application, document) != digest:
            raise RuntimeError("traced and untraced application outputs differ")
        outputs.append((8000, digest))
    else:
        empty = module.AuthRequirement()
        protected = module.AuthRequirement(
            authenticated=True,
            role_checks=(module.SetRequirement(frozenset({"admin"}), "all"),),
        )
        identify = module.AuthRequirement(identify=True)
        public = module.AuthRequirement(public=True)
        cases = (
            ("empty0", (), empty),
            ("empty1", (empty,), empty),
            ("empty2", (empty, empty), empty),
            ("protected", (empty, protected), protected),
            ("identify", (identify, empty), identify),
            ("public", (public, empty), public),
        )
        for name, requirements, expected in cases:
            tracemalloc.start()
            results = [module.merge_requirements(*requirements) for _ in range(10000)]
            retained, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            metrics[f"{name}_retained_bytes"] = retained
            metrics[f"{name}_peak_bytes"] = peak
            if len(results) != 10000 or any(result != expected for result in results):
                raise RuntimeError(f"incorrect merged requirements for {name}")
            del results
            started = time.process_time_ns()
            for _ in range(10000):
                result = module.merge_requirements(*requirements)
            metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
            if result != expected:
                raise RuntimeError(f"incorrect repeated merge for {name}")
            outputs.append((name, repr(result), 10000))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
