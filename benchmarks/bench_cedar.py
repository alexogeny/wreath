"""Microbenchmark Cedar authorization: Wreath's built-in engine vs cedarpy.

Every arm authorizes the same two requests (one allowed, one denied) against
the same six-policy Cedar source and the same entity hierarchy, and every
arm's decisions are verified before anything is timed — the benchmark doubles
as a small conformance check of Wreath's engine against the official Rust
evaluator behind cedarpy.

Two tables, because the arms expose different lifecycles:

- **evaluate**: the policy set is compiled once and evaluation is timed alone.
  Only Wreath offers this split (``CedarPolicies`` compiles at startup), so
  the rows are Wreath-metal and Wreath-pure — an engine-vs-twin comparison,
  not a competitor ranking.
- **parse_and_evaluate**: one stateless call carries parsing and evaluation
  together, which is the only public shape cedarpy offers. Wreath constructs
  ``CedarPolicies`` per call here so both arms pay their full per-call cost.

This measures in-process authorization latency only. No I/O, no policy-store
fetches, and no claim about the engines' relative parsing strictness.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns

from wreath.authorization import CedarEntity, CedarPolicies, EntityUid

POLICY_SOURCE = """
@id("staff-read")
permit(principal in Group::"staff", action == Action::"read", resource);

@id("owner-write")
permit(principal, action == Action::"write", resource)
  when { resource has owner && resource.owner == principal };

@id("public-list")
permit(principal is User, action in [Action::"list", Action::"search"], resource)
  when { context.authenticated };

@id("tag-gate")
permit(principal, action == Action::"read", resource)
  when { resource has tags && resource.tags.contains("public") };

@id("method-guard")
forbid(principal, action, resource)
  unless { context.method == "GET" || action == Action::"write" };

@id("banned")
forbid(principal in Group::"banned", action, resource);
"""

ALICE = EntityUid("User", "alice")
MALLORY = EntityUid("User", "mallory")
DOC = EntityUid("Document", "42")
READ = EntityUid("Action", "read")

ENTITIES = (
    CedarEntity(ALICE, parents=(EntityUid("Group", "staff"),)),
    CedarEntity(EntityUid("Group", "staff"), parents=(EntityUid("Group", "everyone"),)),
    CedarEntity(MALLORY, parents=(EntityUid("Group", "banned"),)),
    CedarEntity(DOC, attrs={"owner": ALICE, "tags": ["internal"]}),
)

# (principal, action, resource, context, expected_allowed)
REQUESTS = (
    (ALICE, READ, DOC, {"method": "GET", "authenticated": True}, True),
    (MALLORY, READ, DOC, {"method": "GET", "authenticated": True}, False),
)


def _cedar_json_entities() -> list[dict]:
    """The same entities in Cedar's standard JSON entity format."""
    entities = []
    for entity in ENTITIES:
        attrs: dict[str, object] = {}
        for key, value in entity.attrs.items():
            if isinstance(value, EntityUid):
                attrs[key] = {"__entity": {"type": value.type, "id": value.id}}
            else:
                attrs[key] = value
        entities.append(
            {
                "uid": {"type": entity.uid.type, "id": entity.uid.id},
                "attrs": attrs,
                "parents": [{"type": p.type, "id": p.id} for p in entity.parents],
            }
        )
    return entities


def _wreath_arms() -> dict[str, Callable[[], int]]:
    from wreath._native import _core
    from wreath._pure import cedar as pure_cedar

    compiled = CedarPolicies(POLICY_SOURCE, entities=ENTITIES)
    policies = compiled._policies
    store = compiled._store
    requests = tuple(
        (
            (principal.type, principal.id),
            (action.type, action.id),
            (resource.type, resource.id),
            dict(context),
            expected,
        )
        for principal, action, resource, context, expected in REQUESTS
    )

    def run_with(evaluate: Callable) -> int:
        allowed = 0
        for principal, action, resource, context, _expected in requests:
            result = evaluate(policies, principal, action, resource, context, store)
            allowed += result[0]
        return allowed

    arms: dict[str, Callable[[], int]] = {
        "wreath-pure": lambda: run_with(pure_cedar.cedar_is_authorized),
    }
    native = getattr(_core, "cedar_is_authorized", None) if _core is not None else None
    if native is not None:
        arms["wreath-metal"] = lambda: run_with(native)
    return arms


def _wreath_stateless() -> Callable[[], int]:
    def authorize() -> int:
        engine = CedarPolicies(POLICY_SOURCE, entities=ENTITIES)
        allowed = 0
        for principal, action, resource, context, _expected in REQUESTS:
            decision = engine.is_authorized(
                principal=principal, action=action, resource=resource, context=context
            )
            allowed += decision.allowed
        return allowed

    return authorize


def _cedarpy_arm() -> Callable[[], int]:
    from cedarpy import Decision, is_authorized

    entities = json.dumps(_cedar_json_entities())
    requests = tuple(
        (
            {
                "principal": str(principal),
                "action": str(action),
                "resource": str(resource),
                "context": dict(context),
            },
            expected,
        )
        for principal, action, resource, context, expected in REQUESTS
    )

    def authorize() -> int:
        allowed = 0
        for request, _expected in requests:
            result = is_authorized(request, POLICY_SOURCE, entities)
            allowed += result.decision == Decision.Allow
        return allowed

    return authorize


_EXPECTED_ALLOWED = sum(expected for *_rest, expected in REQUESTS)


def _verify(name: str, operation: Callable[[], int]) -> None:
    allowed = operation()
    if allowed != _EXPECTED_ALLOWED:
        raise SystemExit(
            f"{name} disagreed with the expected decisions "
            f"({allowed} of {len(REQUESTS)} allowed, expected {_EXPECTED_ALLOWED}); "
            "refusing to time an engine that evaluates differently"
        )


def _measure(operation: Callable[[], int], iterations: int, trials: int) -> list[float]:
    for _ in range(200):
        operation()
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        for _ in range(iterations):
            operation()
        elapsed = perf_counter_ns() - started
        samples.append(elapsed / iterations)
    return samples


def _timed(
    arms: dict[str, Callable[[], int]], iterations: int, trials: int
) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for name, operation in arms.items():
        _verify(name, operation)
        samples = _measure(operation, iterations, trials)
        median = statistics.median(samples)
        results[name] = {
            "median_ns": median,
            "samples_ns": samples,
            "authorizations_per_second": 1_000_000_000 / median,
        }
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--stateless-iterations", type=int, default=2_000)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.stateless_iterations < 1 or args.trials < 3:
        parser.error("iterations must be positive and trials at least 3")

    evaluate = _timed(_wreath_arms(), args.iterations, args.trials)

    stateless_arms: dict[str, Callable[[], int]] = {"wreath": _wreath_stateless()}
    skipped: dict[str, str] = {}
    try:
        stateless_arms["cedarpy"] = _cedarpy_arm()
    except ImportError as error:
        skipped["cedarpy"] = f"not importable: {error}"
    parse_and_evaluate = _timed(stateless_arms, args.stateless_iterations, args.trials)

    document = {
        "tool": "benchmarks.bench_cedar",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "scenario": "six-policy set, two authorizations (one allow, one deny) per call",
        "policies": 6,
        "requests_per_call": len(REQUESTS),
        "iterations": args.iterations,
        "stateless_iterations": args.stateless_iterations,
        "trials": args.trials,
        "evaluate": evaluate,
        "parse_and_evaluate": parse_and_evaluate,
        "skipped": skipped,
        "fairness": (
            "Every arm authorizes the same two requests against the same Cedar source and "
            "entities, and decisions are verified to agree before timing. The evaluate table "
            "is Wreath-only (engine vs pure twin) because cedarpy exposes no precompiled "
            "handle; the parse_and_evaluate table gives both arms their full per-call cost, "
            "including policy parsing. In-process latency only — no I/O and no policy-store "
            "fetch is measured, and a deployed Wreath application pays the evaluate cost, "
            "not the stateless one, because CedarPolicies compiles at startup."
        ),
    }
    payload = json.dumps(document, indent=2) + "\n"
    if args.output is not None:
        from .report import render

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        args.output.with_suffix(".html").write_text(
            render({"metadata": {}, "results": []}, [document]),
            encoding="utf-8",
        )
        print(f"wrote {args.output} and {args.output.with_suffix('.html')}")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
