"""Microbenchmark: native vs pure template rendering.

Compiles one tape per template shape and times the render call (the unit the
request path invokes) for pure and native, interleaved, with an A/A control
fixing the noise floor. Reports per-shape microseconds and speedup — the
evidence that gates keeping the native template engine.

    python -m benchmarks.bench_templates --output benchmark-results-templates/latest.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from typing import Any

from wreath._native import _core
from wreath._pure.templates import (
    Markup,
    TemplateRenderError,
    compile_tape,
    render_tape,
)

_TABLE = (
    "<table>{% for r in rows %}<tr><td>{{ r.id }}</td>"
    "<td>{{ r.message }}</td></tr>{% endfor %}</table>"
)
_CONDITIONAL = (
    "{% for r in rows %}{% if r.active %}<b>{{ r.message }}</b>"
    "{% else %}<i>{{ r.message }}</i>{% endif %}{% endfor %}"
)


def _rows(count: int) -> list[dict[str, Any]]:
    return [
        {"id": i, "message": f"item <{i}> & 'quote' \"x\"", "active": i % 2 == 0}
        for i in range(count)
    ]


# (source, context) per shape. Cells need escaping so the render does real work.
SHAPES: dict[str, tuple[str, dict[str, Any]]] = {
    "table-5": (_TABLE, {"rows": _rows(5)}),
    "table-50": (_TABLE, {"rows": _rows(50)}),
    "conditional-50": (_CONDITIONAL, {"rows": _rows(50)}),
    "plain": ("<h1>{{ title }}</h1><p>{{ body }}</p>", {"title": "A & B", "body": "x<y"}),
}


def _time(fn: Any, iterations: int) -> float:
    start = time.perf_counter()
    fn(iterations)
    return (time.perf_counter() - start) / iterations * 1e6  # us


def run(shape: str, rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    source, context = SHAPES[shape]
    tape = compile_tape(source)
    max_output = 16 * 1024 * 1024
    template_render = _core.template_render
    # Byte parity is the precondition for a meaningful comparison.
    assert render_tape(tape, context) == template_render(tape, context, max_output)

    def pure(n: int) -> None:
        for _ in range(n):
            render_tape(tape, context, max_output)

    def native(n: int) -> None:
        for _ in range(n):
            template_render(tape, context, max_output)

    pure(warmup)
    native(warmup)
    pure_samples: list[float] = []
    native_samples: list[float] = []
    aa_samples: list[float] = []
    for _ in range(rounds):
        pure_samples.append(_time(pure, iterations))
        native_samples.append(_time(native, iterations))
        aa_samples.append(_time(native, iterations))  # A/A twin of native
    pure_median = statistics.median(pure_samples)
    native_median = statistics.median(native_samples)
    floor = abs(native_median - statistics.median(aa_samples))
    return {
        "shape": shape,
        "pure_us": round(pure_median, 4),
        "native_us": round(native_median, 4),
        "speedup": round(pure_median / native_median, 3) if native_median else None,
        "noise_floor_us": round(floor, 4),
        "resolved": abs(pure_median - native_median) > 2 * floor,
        "pure_samples": [round(s, 4) for s in pure_samples],
        "native_samples": [round(s, 4) for s in native_samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", nargs="+", choices=SHAPES, default=list(SHAPES))
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--output")
    args = parser.parse_args()
    if _core is None or not hasattr(_core, "template_render"):
        raise SystemExit("native template engine not built; nothing to compare")
    _core.template_configure(Markup, TemplateRenderError)
    results = [run(shape, args.rounds, args.iterations, args.warmup) for shape in args.shape]
    for entry in results:
        print(
            f"{entry['shape']:15} pure={entry['pure_us']:8.3f}us "
            f"native={entry['native_us']:8.3f}us "
            f"speedup={entry['speedup']:.2f}x "
            f"({'resolved' if entry['resolved'] else 'BELOW NOISE'})"
        )
    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "rounds": args.rounds,
            "iterations": args.iterations,
            "warmup": args.warmup,
        },
        "results": results,
    }
    if args.output:
        from pathlib import Path

        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
