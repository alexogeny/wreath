"""The measurement harness the decomposition tools share.

Every decomposer in `_devtools` answers the same shape of question -- "what does
this piece of a request cost?" -- and every one of them can be wrong in the same
ways. Those lessons live here rather than in each tool:

* **Interleave the arms.** Run them round-robin so thermal and governor drift
  hits every arm, not whichever one ran while the CPU was asleep.
* **Alternate the direction of each round.** Round-robin in a fixed order does
  not remove drift *within* a round, it converts it into a per-arm constant: on
  a powersave governor the CPU ramps across the round, so arm 0 is measured
  cold and arm 7 hot on every single round, and averaging rounds preserves the
  bias exactly. Running odd rounds backwards puts every arm in both positions.
  This is not a refinement -- on an 8-arm round it moved the measured A/A floor
  from 5.61us to 0.18us, and the flattered version had ranked arms in an order
  that was purely their position.
* **Only compare arms measured in the same interleaved run.** Two runs of the
  same arm minutes apart are not comparable: an identical no-op application
  measured 61.67us in a cold first block and 29.71us once the machine had
  ramped. Anything worth comparing goes in as another arm.
* **Measure the noise floor, do not assume it.** An A/A control -- the same
  configuration entered as two separate arms -- placed at the *far end* of the
  round from its twin, so the floor includes within-round drift. Adjacent
  placement flatters it by an order of magnitude.
* **Refuse to report below the floor.** A delta under twice the floor is
  unresolved, not zero. Say so instead of printing a plausible number. This
  matters both ways: an earlier version of the tape decomposition reported a
  *negative* cost for one middleware, and a real ~0.9us fix was dismissed as
  "free" by dividing an unresolvable delta.
* **Check the arm still serves the request.** A middleware that starts
  rejecting -- a drained token bucket is the easy way -- makes its arm faster
  than the baseline, and the decomposition then reports the cost of a 429 as if
  it were the cost of a request. Checked after the run too, because that failure
  develops during it.

Do not reach for `cProfile` on these paths. It adds ~1-2us per call, which is
larger than most of what is being measured, and it has already sent this work
down one wrong path. Ablate instead: remove a piece, time the whole thing.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ROUNDS = 11
DEFAULT_ITERATIONS = 4000
DEFAULT_WARMUP = 2000
#: A delta must clear this multiple of the measured A/A floor to be reported.
RESOLUTION_FACTOR = 2.0


@dataclass
class Arm:
    """One configuration under test, and the samples collected for it."""

    label: str
    app: Any = None
    payload: Any = None
    samples: list[float] = field(default_factory=list)
    #: CPU microseconds per request, filled alongside `samples` by
    #: `measure_apps`. Empty for arms measured through a caller's own loop.
    cpu_samples: list[float] = field(default_factory=list)

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def cpu_median(self) -> float:
        return statistics.median(self.cpu_samples)

    @property
    def p95(self) -> float:
        ordered = sorted(self.samples)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _ordered[T](arms: list[T], round_index: int) -> list[T]:
    """The arms for round `round_index`, reversed on odd rounds.

    Generic because `tape_decomp` carries its own `Arm`, and all this needs of
    one is that a list of them can be reversed.

    A fixed order measures arm 0 at the round's starting clock every time. Over
    an 8-arm round on a powersave governor that positional bias reached 16% of
    the baseline -- larger than most of what these tools measure, and stable
    enough across rounds to look like a result rather than like noise.
    """
    return arms if round_index % 2 == 0 else arms[::-1]


def scope(
    method: str = "GET", path: str = "/", headers: dict[str, str] | None = None
) -> dict[str, Any]:
    raw_path, _, query = path.partition("?")
    items = (headers or {"host": "example.com"}).items()
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in items],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 5555),
        "root_path": "",
        "extensions": {},
    }


async def receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def run(app: Any, template: dict[str, Any], count: int) -> None:
    sent: list[Any] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    for _ in range(count):
        # A fresh dict per request: a real server never hands the same scope
        # twice, and ProxyHeaders mutates it.
        await app(dict(template), receive, send)
        sent.clear()


async def time_app(app: Any, template: dict[str, Any], iterations: int) -> float:
    """Microseconds per request."""
    start = time.perf_counter()
    await run(app, template, iterations)
    return (time.perf_counter() - start) / iterations * 1e6


async def time_app_cpu(
    app: Any, template: dict[str, Any], iterations: int
) -> tuple[float, float]:
    """Wall and CPU microseconds per request, from one pass.

    `time.process_time()` counts user plus system CPU for the whole process and
    excludes anything spent blocked, which is what a burstable instance meters:
    t4g/t3/e2/B-series earn credits at a baseline rate and throttle to it once
    the bucket is empty, so the number that predicts a cliff is CPU per request,
    not latency per request. Wall time on an idle laptop is a decent proxy for
    it and on a loaded server is not one at all.
    """
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    await run(app, template, iterations)
    wall = (time.perf_counter() - wall_start) / iterations * 1e6
    cpu = (time.process_time() - cpu_start) / iterations * 1e6
    return wall, cpu


async def status_of(app: Any, template: dict[str, Any]) -> int:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(dict(template), receive, send)
    return next(
        (
            message.get("status", 0)
            for message in sent
            if message.get("type") in ("http.response.start", "wreath.response")
        ),
        0,
    )


async def verify_serving(arms: list[Arm], template: dict[str, Any], when: str) -> None:
    """Every arm must still answer 200, or its timings describe something else."""
    for arm in arms:
        if arm.app is None:
            continue
        status = await status_of(arm.app, template)
        if status != 200:
            raise SystemExit(
                f"wreath-decomp: arm {arm.label!r} answered {status}, not 200, {when} "
                f"measuring.\nIts timings would be the cost of that response, not of "
                "a served request.\nA rate limiter draining mid-run is the usual cause."
            )


async def measure_apps(
    arms: list[Arm],
    template: dict[str, Any],
    rounds: int = DEFAULT_ROUNDS,
    iterations: int = DEFAULT_ITERATIONS,
    warmup: int = DEFAULT_WARMUP,
) -> None:
    for arm in arms:
        await run(arm.app, template, warmup)
    await verify_serving(arms, template, "before")
    for index in range(rounds):
        # Alternating, so within-round drift does not become a per-arm constant.
        for arm in _ordered(arms, index):
            wall, cpu = await time_app_cpu(arm.app, template, iterations)
            arm.samples.append(wall)
            arm.cpu_samples.append(cpu)
    await verify_serving(arms, template, "after")


def measure_callables(
    arms: list[Arm],
    rounds: int = DEFAULT_ROUNDS,
    iterations: int = 20_000,
    warmup: int = 2_000,
) -> None:
    """For arms whose payload is a plain `fn(n)`, not an ASGI app."""
    for arm in arms:
        arm.payload(warmup)
    for index in range(rounds):
        for arm in _ordered(arms, index):
            start = time.perf_counter()
            arm.payload(iterations)
            arm.samples.append((time.perf_counter() - start) / iterations * 1e6)


def noise_floor(arms: list[Arm], baseline: str, control: str) -> float:
    medians = {arm.label: arm.median for arm in arms}
    return abs(medians[control] - medians[baseline])


def report(
    arms: list[Arm],
    baseline: str,
    control: str,
    *,
    cumulative: bool = False,
    unit: str = "us",
) -> dict[str, Any]:
    """Print a table of arms against `baseline`, flagging unresolved deltas."""
    medians = {arm.label: arm.median for arm in arms}
    base = medians[baseline]
    floor = noise_floor(arms, baseline, control)
    resolution = floor * RESOLUTION_FACTOR

    print(f"baseline ({baseline}) = {base:.2f}{unit}")
    print(
        f"A/A noise floor = {floor:.2f}{unit} ({floor / base * 100:.1f}%); "
        f"a delta must exceed {resolution:.2f}{unit} to be reported\n"
    )
    header = f"{'arm':32s} {'median':>9s} {'vs base':>9s}"
    if cumulative:
        header += f" {'step':>8s}"
    header += "   resolved?"
    print(header)
    print("-" * len(header))

    rows: list[dict[str, Any]] = []
    previous = base
    for arm in arms:
        if arm.label in (baseline, control):
            continue
        delta = arm.median - base
        resolved = abs(delta) > resolution
        line = f"{arm.label:32s} {arm.median:8.2f}{unit} {delta:+8.2f}{unit}"
        if cumulative:
            line += f" {arm.median - previous:+7.2f}{unit}"
        line += f"   {'yes' if resolved else 'BELOW NOISE'}"
        print(line)
        rows.append(
            {
                "arm": arm.label,
                "median": round(arm.median, 3),
                "p95": round(arm.p95, 3),
                "delta": round(delta, 3),
                "step": round(arm.median - previous, 3),
                "resolved": resolved,
            }
        )
        previous = arm.median

    unresolved = [row["arm"] for row in rows if not row["resolved"]]
    if unresolved:
        print(
            f"\n{len(unresolved)} arm(s) did not clear the noise floor. Their cost is "
            "not zero --\nit is unmeasured. Quiet the machine (performance governor, "
            "no background\nload) or raise --rounds/--iterations before attributing "
            "anything to them."
        )
    return {"baseline": round(base, 3), "floor": round(floor, 3), "rows": rows}


def report_cpu(
    arms: list[Arm], baseline: str, control: str, *, requests: int = 1000
) -> dict[str, Any]:
    """Print CPU cost per `requests`, the unit a burstable instance meters.

    Separate from `report` rather than a column on it, so the existing tools'
    output keeps its shape. Read the two together: wall time says what a caller
    waits for, this says what the instance is billed for, and they diverge as
    soon as a request spends time blocked on a socket or a database.

    The `cpu/wall` ratio is the useful third number. Near 1.0 means the arm is
    CPU-bound and every microsecond saved is a microsecond of credit saved;
    well under 1.0 means the request is mostly waiting, and shaving framework
    CPU will not move the throughput ceiling much.
    """
    medians = {arm.label: arm.cpu_median for arm in arms}
    base = medians[baseline]
    floor = abs(medians[control] - base)
    scale = requests / 1000.0

    print(f"baseline ({baseline}) = {base * scale:.2f} CPU-ms / {requests} requests")
    print(
        f"A/A floor = {floor * scale:.2f} ms ({floor / base * 100:.1f}%); "
        f"a delta must exceed {floor * RESOLUTION_FACTOR * scale:.2f} ms\n"
    )
    header = f"{'arm':32s} {'CPU-ms/1k':>11s} {'vs base':>10s} {'cpu/wall':>9s}   resolved?"
    print(header)
    print("-" * len(header))

    rows: list[dict[str, Any]] = []
    for arm in arms:
        if arm.label in (baseline, control):
            continue
        delta = medians[arm.label] - base
        resolved = abs(delta) > floor * RESOLUTION_FACTOR
        ratio = arm.cpu_median / arm.median if arm.median else 0.0
        print(
            f"{arm.label:32s} {medians[arm.label] * scale:10.2f}m "
            f"{delta * scale:+9.2f}m {ratio:9.2f}   "
            f"{'yes' if resolved else 'BELOW NOISE'}"
        )
        rows.append(
            {
                "arm": arm.label,
                "cpu_ms_per_1k": round(medians[arm.label] * scale, 3),
                "delta_ms": round(delta * scale, 3),
                "cpu_wall_ratio": round(ratio, 3),
                "resolved": resolved,
            }
        )
    return {
        "baseline_ms": round(base * scale, 3),
        "floor_ms": round(floor * scale, 3),
        "rows": rows,
    }


def run_apps(
    arms: list[Arm],
    template: dict[str, Any],
    rounds: int,
    iterations: int,
    warmup: int,
) -> None:
    asyncio.run(measure_apps(arms, template, rounds, iterations, warmup))
