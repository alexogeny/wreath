"""Heavy compute: a CP-SAT gear-allocation solver + a joblib model registry.

Idiom: OR-Tools CP-SAT constraint model, and an on-disk `.joblib` + `.meta.json`
model registry with a dtype/bounds contract.
"""
from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path

import joblib
from ortools.sat.python import cp_model


class ModelRegistry:
    def __init__(self, root: str) -> None:
        self._root = Path(root)

    @cached_property
    def meta(self) -> dict:
        return json.loads((self._root / "model.meta.json").read_text())

    def load(self):
        return joblib.load(self._root / "model.joblib")


def allocate_gear(weights: list[int], capacity: int) -> list[int]:
    """Maximise loaded weight under a capacity constraint; return the chosen indices."""
    model = cp_model.CpModel()
    take = [model.NewBoolVar(f"take_{i}") for i in range(len(weights))]
    model.Add(sum(w * t for w, t in zip(weights, take)) <= capacity)
    model.Maximize(sum(w * t for w, t in zip(weights, take)))
    solver = cp_model.CpSolver()
    solver.Solve(model)
    return [i for i, t in enumerate(take) if solver.Value(t)]


def run_plan(plan_id: str) -> str:
    return json.dumps({"plan": plan_id, "picked": allocate_gear([12, 7, 5, 20], 25)})
