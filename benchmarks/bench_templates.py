"""Microbenchmark: walking a template tape against running a compiled program.

`template_render` executes the tape directly; `template_render_compiled` lowers
it to a native program first and then runs that. Both are C and both produce the
same bytes, so this measures what the lowering buys.

Compiles one tape per template shape and times the render call (the unit the
request path invokes) for each arm, interleaved, with an A/A control fixing the
noise floor. Reports per-shape microseconds and speedup — the evidence that
gates keeping the compile step.

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
from wreath._template_tape import Markup, TemplateRenderError, compile_tape

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
    template_render_compiled = _core.template_render_compiled
    program = _core.template_compile(tape)
    # Identical bytes are the precondition for a meaningful comparison: the two
    # arms are the same tape executed two ways, and if they diverged the faster
    # one would be measuring a different job.
    assert template_render(tape, context, max_output) == template_render_compiled(
        program, context, max_output
    )

    def walked(n: int) -> None:
        for _ in range(n):
            template_render(tape, context, max_output)

    def compiled(n: int) -> None:
        for _ in range(n):
            template_render_compiled(program, context, max_output)

    walked(warmup)
    compiled(warmup)
    walked_samples: list[float] = []
    compiled_samples: list[float] = []
    aa_samples: list[float] = []
    for _ in range(rounds):
        walked_samples.append(_time(walked, iterations))
        compiled_samples.append(_time(compiled, iterations))
        aa_samples.append(_time(compiled, iterations))  # A/A against `compiled`
    walked_median = statistics.median(walked_samples)
    compiled_median = statistics.median(compiled_samples)
    floor = abs(compiled_median - statistics.median(aa_samples))
    return {
        "shape": shape,
        "walked_us": round(walked_median, 4),
        "compiled_us": round(compiled_median, 4),
        "speedup": round(walked_median / compiled_median, 3) if compiled_median else None,
        "noise_floor_us": round(floor, 4),
        "resolved": abs(walked_median - compiled_median) > 2 * floor,
        "walked_samples": [round(s, 4) for s in walked_samples],
        "compiled_samples": [round(s, 4) for s in compiled_samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", nargs="+", choices=SHAPES, default=list(SHAPES))
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--output")
    args = parser.parse_args()
    _core.template_configure(Markup, TemplateRenderError)
    results = [run(shape, args.rounds, args.iterations, args.warmup) for shape in args.shape]
    for entry in results:
        print(
            f"{entry['shape']:15} walked={entry['walked_us']:8.3f}us "
            f"compiled={entry['compiled_us']:8.3f}us "
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
