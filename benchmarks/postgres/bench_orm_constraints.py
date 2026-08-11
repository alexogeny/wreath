"""Measure business rules on the write path against hand-written Python and pydantic.

The model is an ``Intern``: an ``Employee`` whose salary is capped at 50,000 and
whose tenure is capped at 8 months, plus a rule that spans the two. Every
contender does the same job -- prove a JSON-shaped body, report *every* bad
field with its location, and produce an object -- so the numbers are comparable.

Three contenders:

* ``wreath`` compiles one validator per column, fusing the column's type with its
  checks, and generates it as straight-line source. A bound is a comparison, not
  a call.
* ``handwritten`` is the same rules written out by hand, the way you would write
  them with no framework at all. This is the floor a validation system has to
  justify itself against, not a strawman: it is specialized to this one model.
* ``pydantic`` is the same model as a ``BaseModel``, doing its validation in
  Rust.

**Fairness.** Wreath's column types are strict: ``"5"`` is not an ``int8`` and
``True`` is not one either. Pydantic is lax by default and would accept both, so
it is configured ``strict=True`` -- otherwise it would be solving a different
and harder problem. It is also ``extra="forbid"``, because wreath rejects unknown
fields. The hand-written contender is strict for the same reason.

Both the accepting and the rejecting path are measured. A validator is rarely
only asked about good input, and the cost of *refusing* is what a public
endpoint pays under abuse.

pydantic is a benchmark dependency and never a dependency of wreath:
``uv sync --group benchmark --inexact``. Needs no database.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

from wreath.binding import ValidationError
from wreath.orm import Ge, Le, Length, Mapped, Model, OneOf, column, narrow, rule
from wreath.orm.types import Int64, Text
from wreath.orm.validation import compile_model_validator

try:
    import pydantic
except ImportError:  # pragma: no cover - reported, not raised
    pydantic = None  # type: ignore[assignment]


GRADES = ("intern", "junior", "senior")

# -- wreath -----------------------------------------------------------------------


class Employee(Model):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, check=Length(1, 200))
    salary: Mapped[int] = column(Int64, check=Ge(0))
    tenure_months: Mapped[int] = column(Int64, check=Ge(0))
    grade: Mapped[str] = column(Text, check=OneOf(*GRADES))


class Intern(Employee, table="constraint_bench_interns"):
    salary_cap = narrow("salary", Le(50_000))
    tenure_cap = narrow("tenure_months", Le(8))
    grade_fixed = narrow("grade", OneOf("intern"))

    @rule("salary", "tenure_months", at="salary")
    def pay_band(salary: int, tenure_months: int) -> bool:
        """an intern past six months cannot be paid more than 40k"""
        return not (tenure_months > 6 and salary > 40_000)


# -- hand-written Python -------------------------------------------------------


class HandIntern:
    """What the wreath model is, without the model: five proven values."""

    __slots__ = ("grade", "name", "salary", "tenure_months")

    def __init__(self, name: str, salary: int, tenure_months: int, grade: str) -> None:
        self.name = name
        self.salary = salary
        self.tenure_months = tenure_months
        self.grade = grade


_INT64_LOW, _INT64_HIGH = -(2**63), 2**63


def handwritten(payload: dict[str, Any]) -> HandIntern:
    """The same rules, specialized to this one model by hand.

    Written the way a careful person writes a validator with no framework:
    every field checked once, every error collected with its location, the
    cross-field rule last.
    """
    errors: list[dict[str, Any]] = []

    name = payload.get("name")
    if type(name) is not str:
        errors.append({"loc": ["body", "name"], "msg": "expected str", "type": "text"})
    elif not 1 <= len(name) <= 200:
        errors.append(
            {"loc": ["body", "name"], "msg": "length must be between 1 and 200",
             "type": "length"}
        )

    salary = payload.get("salary")
    if type(salary) is not int:
        errors.append({"loc": ["body", "salary"], "msg": "expected int8", "type": "int8"})
    elif not _INT64_LOW <= salary < _INT64_HIGH:
        errors.append({"loc": ["body", "salary"], "msg": "out of range", "type": "int8"})
    elif salary < 0:
        errors.append(
            {"loc": ["body", "salary"], "msg": "value must be at least 0", "type": "ge"}
        )
    elif salary > 50_000:
        errors.append(
            {"loc": ["body", "salary"], "msg": "value must be at most 50000", "type": "le"}
        )

    tenure = payload.get("tenure_months")
    if type(tenure) is not int:
        errors.append(
            {"loc": ["body", "tenure_months"], "msg": "expected int8", "type": "int8"}
        )
    elif not _INT64_LOW <= tenure < _INT64_HIGH:
        errors.append(
            {"loc": ["body", "tenure_months"], "msg": "out of range", "type": "int8"}
        )
    elif tenure < 0:
        errors.append(
            {"loc": ["body", "tenure_months"], "msg": "value must be at least 0",
             "type": "ge"}
        )
    elif tenure > 8:
        errors.append(
            {"loc": ["body", "tenure_months"], "msg": "value must be at most 8",
             "type": "le"}
        )

    grade = payload.get("grade")
    if type(grade) is not str:
        errors.append({"loc": ["body", "grade"], "msg": "expected str", "type": "text"})
    elif grade != "intern":
        errors.append(
            {"loc": ["body", "grade"], "msg": "value must be one of 'intern'",
             "type": "one_of"}
        )

    for key in payload:
        if key not in ("name", "salary", "tenure_months", "grade"):
            errors.append({"loc": ["body", key], "msg": "unexpected field", "type": "extra"})

    if errors:
        raise ValidationError(errors)

    if tenure > 6 and salary > 40_000:  # type: ignore[operator]
        raise ValidationError(
            [
                {
                    "loc": ["body", "salary"],
                    "msg": "an intern past six months cannot be paid more than 40k",
                    "type": "pay_band",
                }
            ]
        )
    return HandIntern(name, salary, tenure, grade)  # type: ignore[arg-type]


# -- pydantic ------------------------------------------------------------------


def _build_pydantic() -> Any:
    if pydantic is None:
        return None
    from typing import Literal

    from pydantic import BaseModel, ConfigDict, Field, model_validator

    class PydanticIntern(BaseModel):
        # strict=True to match wreath's column types, which never coerce "5" to 5;
        # extra="forbid" to match wreath rejecting unknown fields.
        model_config = ConfigDict(strict=True, extra="forbid")

        name: str = Field(min_length=1, max_length=200)
        salary: int = Field(ge=0, le=50_000)
        tenure_months: int = Field(ge=0, le=8)
        grade: Literal["intern"]

        @model_validator(mode="after")
        def pay_band(self) -> PydanticIntern:
            if self.tenure_months > 6 and self.salary > 40_000:
                raise ValueError("an intern past six months cannot be paid more than 40k")
            return self

    return PydanticIntern


# -- payloads ------------------------------------------------------------------

ACCEPTED = {"name": "Ada Lovelace", "salary": 30_000, "tenure_months": 3, "grade": "intern"}
#: Breaks the salary cap and the grade, and is otherwise sound: the rejecting
#: path with more than one error to collect.
REJECTED = {"name": "Ada Lovelace", "salary": 60_000, "tenure_months": 3, "grade": "senior"}


def _rejects(
    operation: Callable[[dict[str, Any]], Any],
    payload: dict[str, Any],
    errors_of: Callable[[Any], Any],
) -> Callable[[], Any]:
    """Time a refusal *including* materializing the errors it reports.

    ``errors_of`` is not a formality. pydantic raises from Rust and builds its
    error list only when ``.errors()`` is called, so timing the raise alone
    would credit it for work it has not done yet, against contenders that have
    already built theirs. A 422 needs the list, so every contender is made to
    produce it.
    """

    def reject() -> Any:
        try:
            operation(payload)
        except Exception as error:  # noqa: BLE001 - every contender raises its own
            return errors_of(error)
        raise RuntimeError("a body that breaks two rules was accepted")

    return reject


# -- harness -------------------------------------------------------------------


def _measure(operation: Callable[[], Any], warmup: int, trials: int, rows: int) -> list[float]:
    for _ in range(warmup):
        operation()
    samples = []
    for _ in range(trials):
        gc.collect()
        started = time.perf_counter()
        for _ in range(rows):
            operation()
        samples.append(time.perf_counter() - started)
    return samples


def _retained(operation: Callable[[], Any], rows: int) -> dict[str, int]:
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    held = [operation() for _ in range(rows)]
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    stats = after.compare_to(before, "filename")
    result = {
        "retained_blocks": sum(item.count_diff for item in stats),
        "retained_bytes": sum(item.size_diff for item in stats),
    }
    del held
    return result


def _summary(samples: list[float], rows: int) -> dict[str, object]:
    ordered = sorted(samples)
    median = statistics.median(samples)
    return {
        "median_seconds": median,
        "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "bodies_per_second": rows / median,
        "raw_seconds": samples,
    }


def _agree(
    contenders: dict[str, Callable[[dict[str, Any]], Any]],
    errors_of: dict[str, Callable[[Any], Any]],
) -> None:
    """Refuse to report numbers for contenders that are not doing the same job.

    Both directions matter. Accepting the same body proves they agree on what is
    valid; rejecting it for *the same two fields* proves the rejecting benchmark
    is not timing one contender finding two problems against another finding one.
    """
    for name, operation in contenders.items():
        result = operation(ACCEPTED)
        for field, expected in ACCEPTED.items():
            actual = getattr(result, field)
            if actual != expected:
                raise RuntimeError(f"{name} disagrees on {field}: {actual!r} != {expected!r}")
        try:
            operation(REJECTED)
        except Exception as error:  # noqa: BLE001 - each contender raises its own type
            reported = errors_of[name](error)
            fields = sorted(
                str(item["loc"][-1]) for item in reported  # type: ignore[index]
            )
            if fields != ["grade", "salary"]:
                raise RuntimeError(
                    f"{name} reported {fields} for the rejected body, not "
                    "['grade', 'salary']; the contenders are not solving the "
                    "same problem"
                ) from None
            continue
        raise RuntimeError(f"{name} accepted a body that breaks two rules")


def run(args: argparse.Namespace) -> int:
    validate_body = compile_model_validator(Intern)
    pydantic_model = _build_pydantic()

    contenders: dict[str, Callable[[dict[str, Any]], Any]] = {
        "wreath": lambda payload: validate_body(payload, ("body",)),
        "handwritten": handwritten,
    }
    # How each contender hands back the errors a 422 would render.
    errors_of: dict[str, Callable[[Any], Any]] = {
        "wreath": lambda error: error.errors,
        "handwritten": lambda error: error.errors,
    }
    if pydantic_model is not None:
        contenders["pydantic"] = pydantic_model.model_validate
        errors_of["pydantic"] = lambda error: error.errors()
    _agree(contenders, errors_of)

    results: dict[str, dict[str, Any]] = {}
    for name, operation in contenders.items():
        accept = lambda payload=ACCEPTED, op=operation: op(payload)  # noqa: E731
        results[name] = {
            "accept": {
                **_summary(_measure(accept, args.warmup, args.trials, args.bodies), args.bodies),
                **_retained(accept, args.bodies),
            },
            "reject": _summary(
                _measure(
                    _rejects(operation, REJECTED, errors_of[name]),
                    args.warmup,
                    args.trials,
                    args.bodies,
                ),
                args.bodies,
            ),
        }

    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "model_basicsize": Intern.__basicsize__,
            "pydantic": getattr(pydantic, "VERSION", None),
            "bodies": args.bodies,
            "columns": 4,
            "checks": "3 field bounds + 1 enum + 1 length + 1 cross-field rule",
            "warmup": args.warmup,
            "trials": args.trials,
        },
        "results": results,
        "speedup_over_wreath": {
            scenario: {
                name: results[name][scenario]["median_seconds"]
                / results["wreath"][scenario]["median_seconds"]
                for name in results
            }
            for scenario in ("accept", "reject")
        },
    }
    print(json.dumps(document, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bodies", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=1_000)
    parser.add_argument("--trials", type=int, default=5)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
