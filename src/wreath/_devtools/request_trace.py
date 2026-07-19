"""Trace the Python/native boundary crossings of one request lifecycle.

The other native tools read C source (`wreath-native-lint`, `-boundary-lint`) or
sample a whole process (`wreath-native-profile`). Neither can answer the question
this one exists for: *for a single request, what runs in Python, in what order,
and how much of it runs before the route handler is activated?*

Wreath's intended shape is that ingress, routing, authentication, and
authorization are native, and Python is entered when a route is activated.
Every crossing this reports before the `handler` phase is a departure from that
shape -- some of them necessary (a user middleware hook is Python by
definition), some of them not.

    uv run wreath-request-trace
    uv run wreath-request-trace --app myapp.main:app --path /items/42
    uv run wreath-request-trace --verbose
    uv run wreath-request-trace --format json

The sample app's trace is checkpointed in `docs/agents/request-boundary-baseline.json`.
`--check` re-measures and diffs against it, so a change that adds pre-activation
Python shows up as a reviewable number rather than as drift nobody noticed:

    uv run wreath-request-trace --check            # 1 if crossings grew
    uv run wreath-request-trace --update-baseline  # after an intentional change

Crossing the baseline is a trade-off, not a defect. Record why in the commit
that raises it.

`sys.setprofile` reports `c_call` when Python calls a C function and `call`
when a Python frame is entered, so this counts crossings exactly rather than
estimating them. Two limits follow from that and are worth stating plainly:
it sees the framework layer (from the ASGI callable inward), not the server's
own C parse loop, and it cannot see C calling into C. It counts crossings; it
does not time them. Use `wreath-native-profile` for attribution.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType
from typing import Any

from .native_lint import repo_root
from .sample_app import SCENARIOS

#: Checked in, and reviewed like any other contract. See `--check`.
BASELINE_PATH = Path("docs/agents/request-boundary-baseline.json")

# Landmarks in `wreath.app` that open a lifecycle phase. Keyed by code name, which
# is stable across the private reshuffling these internals are subject to.
_PHASE_LANDMARKS: dict[str, str] = {
    "_handle_http": "ingress",
    "_wreath_http": "ingress",
    "__call__": "ingress",
    "authenticate": "auth",
    "_authorize_request": "auth",
    "_identity_mask": "auth",
    "_run_stage": "middleware",
    "_finish_http": "egress",
}
# C routing entry points, which are the phase rather than being called from it.
_ROUTING_CALLS = frozenset({"classify", "resolve", "match", "probe"})

_PHASE_ORDER = ("ingress", "middleware", "routing", "auth", "handler", "egress")
#: Phases that run before a route handler is activated.
_PRE_ACTIVATION = ("ingress", "middleware", "routing", "auth")


@dataclass
class Event:
    phase: str
    kind: str  # "C" or "PY"
    name: str


@dataclass
class Trace:
    events: list[Event] = field(default_factory=list)
    c_calls: Counter[str] = field(default_factory=Counter)
    py_calls: Counter[str] = field(default_factory=Counter)

    @property
    def pre_activation(self) -> list[Event]:
        activated = False
        result: list[Event] = []
        for event in self.events:
            if event.phase == "handler":
                activated = True
                break
            result.append(event)
        del activated
        return result


class _Tracer:
    """Attribute each boundary crossing to a lifecycle phase.

    Phase is a property of *where the request is*, not of the frame stack, so
    it is latched on entering a landmark and only moves forward. Handler
    activation is detected from the route table rather than by name, so it
    stays correct for any application.
    """

    def __init__(self, handler_codes: frozenset[CodeType]) -> None:
        self._handler_codes = handler_codes
        self._phase = "ingress"
        self._activated = False
        self.trace = Trace()
        self.enabled = False

    def _record(self, kind: str, name: str) -> None:
        self.trace.events.append(Event(self._phase, kind, name))
        counter = self.trace.c_calls if kind == "C" else self.trace.py_calls
        counter[name] += 1

    def __call__(self, frame: Any, event: str, arg: Any) -> None:
        if not self.enabled:
            return
        # The harness is not the request. Anything this module does on its own
        # frames -- building a scope, collecting sent messages -- would
        # otherwise be counted against the app under trace.
        if _is_harness_frame(frame.f_code):
            return
        if event == "c_call":
            name = getattr(arg, "__qualname__", None) or getattr(arg, "__name__", "?")
            module = getattr(arg, "__module__", "") or ""
            # A route table's classify/resolve is the routing phase itself.
            if name.split(".")[-1] in _ROUTING_CALLS and "Route" in name:
                self._phase = "routing"
            self._record("C", f"{module}.{name}" if module else name)
            return
        if event != "call":
            return
        code = frame.f_code
        if code in self._handler_codes:
            self._phase = "handler"
            self._activated = True
        elif not self._activated:
            landmark = _PHASE_LANDMARKS.get(code.co_name)
            if landmark is not None:
                self._phase = landmark
            elif code.co_name in ("before", "handle_preflight"):
                self._phase = "middleware"
        elif code.co_name == "_finish_http":
            self._phase = "egress"
        # A Python-to-Python call is not a native boundary crossing. Record only
        # entries whose caller is outside the traced application (typically a
        # task, protocol, or other C-owned callback). Phase landmarks above must
        # still observe every frame so attribution remains exact.
        caller = frame.f_back
        caller_is_traced_python = caller is not None and (
            _is_wreath_frame(caller.f_code) or caller.f_code in self._handler_codes
        )
        if (
            (_is_wreath_frame(code) or code in self._handler_codes)
            and not caller_is_traced_python
        ):
            self._record("PY", f"{_short_path(code)}:{code.co_name}")


def _is_harness_frame(code: CodeType) -> bool:
    """True for this module's own frames, but not for a traced sample app."""
    filename = code.co_filename
    return filename.endswith("_devtools/request_trace.py")


def _is_wreath_frame(code: CodeType) -> bool:
    return "/neo/" in code.co_filename and not _is_harness_frame(code)


def _short_path(code: CodeType) -> str:
    filename = code.co_filename
    if "/neo/" in filename:
        return filename.rsplit("/neo/", 1)[-1]
    return filename.rsplit("/", 1)[-1]


def _handler_codes(app: Any) -> frozenset[CodeType]:
    """Every code object that counts as 'the route was activated'.

    Both the declared handler and the compiled endpoint qualify: when a binder
    wraps the handler, the binder frame is part of activating the route, not
    work done before it.
    """
    codes: set[CodeType] = set()
    for source in (getattr(app, "_routes", ()), ()):
        for definition in source:
            handler = getattr(definition, "handler", None)
            code = getattr(handler, "__code__", None)
            if code is not None:
                codes.add(code)
    for compiled in getattr(app, "_handler_requirements", {}):
        code = getattr(compiled, "__code__", None)
        if code is not None:
            codes.add(code)
    return frozenset(codes)


def _load_app(target: str) -> Any:
    module_name, separator, attribute = target.partition(":")
    if not separator:
        raise SystemExit(f"wreath-request-trace: --app must be 'module:attribute', got {target!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise SystemExit(
            f"wreath-request-trace: {module_name} has no attribute {attribute!r}"
        ) from None


def _scope(method: str, path: str, headers: dict[str, str]) -> dict[str, Any]:
    raw_path, _, query = path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 5555),
        "root_path": "",
        "extensions": {},
    }


async def _drive(app: Any, method: str, path: str, headers: dict[str, str]) -> tuple[Trace, int]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    # Warm first: route compilation and one-shot caches are startup costs, and
    # counting them would describe a request nobody serves.
    await app(_scope(method, path, headers), receive, send)
    status = next(
        (
            message.get("status", 0)
            for message in sent
            if message.get("type") in ("http.response.start", "wreath.response")
        ),
        0,
    )
    sent.clear()

    # Built before the profiler is armed: a real server hands the app a scope it
    # already parsed, so charging the request for building one would inflate
    # ingress with work the app never does.
    scope = _scope(method, path, headers)
    tracer = _Tracer(_handler_codes(app))
    sys.setprofile(tracer)
    tracer.enabled = True
    try:
        await app(scope, receive, send)
    finally:
        tracer.enabled = False
        sys.setprofile(None)
    return tracer.trace, status


def trace_request(
    app: Any, method: str, path: str, headers: dict[str, str]
) -> tuple[Trace, int]:
    """Count one request's boundary crossings. Returns (trace, response status)."""
    return asyncio.run(_drive(app, method, path, headers))


def _render_text(trace: Trace, status: int, verbose: bool) -> None:
    by_phase_c: Counter[str] = Counter()
    by_phase_py: Counter[str] = Counter()
    for event in trace.events:
        counter = by_phase_c if event.kind == "C" else by_phase_py
        counter[event.phase] += 1

    total_c = sum(trace.c_calls.values())
    total_py = sum(trace.py_calls.values())
    pre_c = sum(by_phase_c[phase] for phase in _PRE_ACTIVATION)
    pre_py = sum(by_phase_py[phase] for phase in _PRE_ACTIVATION)

    print(f"response status: {status}")
    print(f"\n{'phase':12s} {'into C':>8s} {'Python frames':>14s}")
    for phase in _PHASE_ORDER:
        if by_phase_c[phase] or by_phase_py[phase]:
            marker = "  <- before activation" if phase in _PRE_ACTIVATION else ""
            print(f"{phase:12s} {by_phase_c[phase]:8d} {by_phase_py[phase]:14d}{marker}")
    print(f"{'total':12s} {total_c:8d} {total_py:14d}")

    print(
        f"\nBefore the route handler is activated: {pre_py} Python entry boundary/boundaries "
        f"and {pre_c} call(s) into C."
    )
    if pre_py:
        print(
            "Wreath's intended shape is that ingress, routing, and authorization stay "
            "native.\nEach frame below is Python running before a route was activated:"
        )
        seen: Counter[str] = Counter()
        for event in trace.events:
            if event.phase == "handler":
                break
            if event.kind == "PY":
                seen[event.name] += 1
        for name, count in seen.most_common():
            print(f"  {count:4d}  {name}")

    print("\nMost-called C entry points:")
    for name, count in trace.c_calls.most_common(12):
        print(f"  {count:4d}  {name}")

    if verbose:
        print("\nOrdered trace:")
        for event in trace.events:
            print(f"  [{event.phase:10s}] {event.kind:2s} {event.name}")


_BASELINE_NOTE = (
    "Python/native boundary crossings for one request through each scenario, "
    "measured by `uv run wreath-request-trace`. Wreath's intended shape is that ingress, "
    "routing, authentication, and authorization stay native, so `pre_activation` -- "
    "work done before a route handler is activated -- is the number that matters. "
    "Regenerate with `uv run wreath-request-trace --update-baseline`; `--check` fails "
    "when a scenario grows. Growth is a trade-off, not automatically a defect: an "
    "app-visible feature may be worth crossings. Justify it in the commit message "
    "that raises these numbers, and prefer paying in `handler` over `pre_activation`."
)


def _summarize(trace: Trace, status: int) -> dict[str, Any]:
    by_phase: dict[str, dict[str, int]] = {}
    for event in trace.events:
        bucket = by_phase.setdefault(event.phase, {"c": 0, "python": 0})
        bucket["c" if event.kind == "C" else "python"] += 1
    return {
        "status": status,
        "totals": {
            "c": sum(trace.c_calls.values()),
            "python": sum(trace.py_calls.values()),
        },
        "pre_activation": {
            "c": sum(by_phase.get(phase, {}).get("c", 0) for phase in _PRE_ACTIVATION),
            "python": sum(
                by_phase.get(phase, {}).get("python", 0) for phase in _PRE_ACTIVATION
            ),
        },
        "phases": {phase: by_phase[phase] for phase in _PHASE_ORDER if phase in by_phase},
        "c_calls": dict(trace.c_calls.most_common()),
        "python_frames": dict(trace.py_calls.most_common()),
    }


def _baseline_path() -> Path:
    return repo_root() / BASELINE_PATH


def _measure_scenarios() -> dict[str, Any]:
    scenarios: dict[str, Any] = {}
    for name, builder in SCENARIOS.items():
        app, headers, method, path = builder()
        trace, status = trace_request(app, method, path, headers)
        summary = _summarize(trace, status)
        summary["request"] = {"method": method, "path": path}
        scenarios[name] = summary
    return {"note": _BASELINE_NOTE, "scenarios": scenarios}


def _write_baseline() -> int:
    path = _baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _measure_scenarios()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wreath-request-trace: wrote {BASELINE_PATH}")
    for name, summary in payload["scenarios"].items():
        pre = summary["pre_activation"]
        print(f"  {name:10s} pre-activation: {pre['python']} Python, {pre['c']} C")
    return 0


def _check_baseline() -> int:
    path = _baseline_path()
    if not path.exists():
        print(
            f"wreath-request-trace: no baseline at {BASELINE_PATH}; "
            "create it with --update-baseline",
            file=sys.stderr,
        )
        return 1
    recorded = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    current = _measure_scenarios()["scenarios"]
    regressions = 0

    for name, summary in current.items():
        before = recorded.get(name)
        if before is None:
            print(f"{name}: not in the baseline; run --update-baseline")
            regressions += 1
            continue
        for section in ("pre_activation", "totals"):
            for kind in ("python", "c"):
                new = summary[section][kind]
                old = before[section][kind]
                if new == old:
                    continue
                delta = new - old
                label = f"{name}.{section}.{kind}"
                if delta > 0:
                    regressions += 1
                    print(f"  grew    {label}: {old} -> {new} (+{delta})")
                else:
                    print(f"  shrank  {label}: {old} -> {new} ({delta})")

    if regressions:
        print(
            f"\nwreath-request-trace: {regressions} increase(s) over {BASELINE_PATH}.\n"
            "Each one is Python or boundary work a request did not do before. If the "
            "feature\nis worth it, say why in the commit and re-record with "
            "--update-baseline.",
            file=sys.stderr,
        )
        return 1
    print(f"wreath-request-trace: no crossings added over {BASELINE_PATH}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-request-trace",
        description="Trace Python/native boundary crossings across one request lifecycle.",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        default="realistic",
        help="built-in app to trace (default: realistic)",
    )
    parser.add_argument(
        "--app",
        help="trace your own ASGI app, as 'module:attribute', instead of a scenario",
    )
    parser.add_argument("--method", default=None)
    parser.add_argument("--path", default=None, help="request path (default: the scenario's)")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="repeatable request header",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--verbose", action="store_true", help="print the ordered per-event trace"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"diff every scenario against {BASELINE_PATH}; exit 1 if crossings grew",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help=f"re-record {BASELINE_PATH} from every scenario",
    )
    args = parser.parse_args(argv)

    if args.check and args.update_baseline:
        raise SystemExit("wreath-request-trace: --check and --update-baseline are exclusive")
    if args.update_baseline:
        return _write_baseline()
    if args.check:
        return _check_baseline()

    if args.app:
        app = _load_app(args.app)
        headers: dict[str, str] = {"host": "example.com"}
        method, path = args.method or "GET", args.path or "/"
    else:
        app, headers, default_method, default_path = SCENARIOS[args.scenario]()
        method = args.method or default_method
        path = args.path or default_path

    for item in args.header:
        name, separator, value = item.partition(":")
        if not separator:
            raise SystemExit(f"wreath-request-trace: --header wants NAME:VALUE, got {item!r}")
        headers[name.strip().lower()] = value.strip()

    trace, status = trace_request(app, method, path, headers)

    if args.format == "json":
        summary = _summarize(trace, status)
        summary["events"] = [
            {"phase": e.phase, "kind": e.kind, "name": e.name} for e in trace.events
        ]
        print(json.dumps(summary, indent=2))
    else:
        _render_text(trace, status, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
