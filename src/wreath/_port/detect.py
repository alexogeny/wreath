"""What framework is this tree written in, and can `wreath port` port it?

* **Nothing recognized, exit 0.** An aiohttp, Tornado or Pyramid tree produced no
  findings, `coverage_overall: null`, and a successful exit. Nothing in that
  output distinguishes "I read this and it needs no work" from "I have no idea
  what this is".
* **A familiar decorator in an unfamiliar framework.** Bottle and FastAPI both
  spell routes as `@app.get(...)`, so framework identity gates confidence.

So detection runs first and is reported next to the coverage number. It answers
from imports alone -- no execution, no installed packages -- which is enough to
name a framework and, crucially, enough to say *this is not the one I port*.

Concurrency is tracked separately from the framework, because `monkey.patch_all()`
is not a framework and changes more than one. Under it, an ordinary-looking
blocking call is a yield point, and a mechanical rewrite to `async def` produces
code that passes its tests at low concurrency and serialises in production. That
is a refusal, not a low score.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Root package -> the name a human calls it. Only web frameworks: the question
# is "what serves the requests", not "what is installed".
FRAMEWORKS = {
    "fastapi": "FastAPI",
    "starlette": "Starlette",
    "aiohttp": "aiohttp",
    "blacksheep": "BlackSheep",
    "bottle": "Bottle",
    "cherrypy": "CherryPy",
    "django": "Django",
    "falcon": "Falcon",
    "flask": "Flask",
    "litestar": "Litestar",
    "pyramid": "Pyramid",
    "quart": "Quart",
    "sanic": "Sanic",
    "starlite": "Starlite",
    "tornado": "Tornado",
    "twisted": "Twisted",
    "web": "web.py",
}

# The two this tool actually translates. Everything else is reported, not ported.
TARGETS = frozenset({"fastapi", "starlette"})

# Not frameworks -- concurrency models that reinterpret every call beneath them.
RUNTIMES = {"gevent": "gevent", "eventlet": "eventlet"}

# `monkey.patch_all()`, `eventlet.monkey_patch()`. Matched on the call name so a
# module that imports the patcher under any alias is still caught.
_PATCH_CALLS = frozenset({"patch_all", "monkey_patch"})


@dataclass(frozen=True)
class ModuleSignals:
    """What one module's imports say about the stack it belongs to."""

    roots: frozenset[str]
    patch_line: int | None = None


def scan_module(tree: ast.AST) -> ModuleSignals:
    """Read one parsed module's imports. Never imports or executes anything."""
    roots: set[str] = set()
    patch_line: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # A relative import has no root package of its own to report.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and patch_line is None:
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name in _PATCH_CALLS:
                patch_line = node.lineno
    return ModuleSignals(frozenset(roots), patch_line)


@dataclass(frozen=True)
class Detection:
    """The stack a tree appears to be, aggregated over its modules."""

    modules: int = 0
    frameworks: dict[str, int] = field(default_factory=dict)
    runtimes: dict[str, int] = field(default_factory=dict)
    patch_sites: tuple[tuple[str, int], ...] = ()

    @classmethod
    def of(cls, signals: dict[str, ModuleSignals]) -> Detection:
        frameworks: dict[str, int] = {}
        runtimes: dict[str, int] = {}
        patches: list[tuple[str, int]] = []
        for path, sig in signals.items():
            for root in sig.roots:
                if root in FRAMEWORKS:
                    frameworks[root] = frameworks.get(root, 0) + 1
                if root in RUNTIMES:
                    runtimes[root] = runtimes.get(root, 0) + 1
            if sig.patch_line is not None and sig.roots & RUNTIMES.keys():
                patches.append((path, sig.patch_line))
        return cls(
            modules=len(signals),
            frameworks=dict(sorted(frameworks.items())),
            runtimes=dict(sorted(runtimes.items())),
            patch_sites=tuple(sorted(patches)),
        )

    @classmethod
    def merge(cls, parts) -> Detection | None:
        """Combine per-root detections for a multi-root run.

        `None` when no part carried one, so "several roots, none asked" stays
        distinguishable from "several roots, nothing found".
        """
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        frameworks: dict[str, int] = {}
        runtimes: dict[str, int] = {}
        sites: list[tuple[str, int]] = []
        for part in parts:
            for root, n in part.frameworks.items():
                frameworks[root] = frameworks.get(root, 0) + n
            for root, n in part.runtimes.items():
                runtimes[root] = runtimes.get(root, 0) + n
            sites.extend(part.patch_sites)
        return cls(
            modules=sum(p.modules for p in parts),
            frameworks=dict(sorted(frameworks.items())),
            runtimes=dict(sorted(runtimes.items())),
            patch_sites=tuple(sorted(set(sites))),
        )

    @property
    def target_modules(self) -> int:
        return sum(n for root, n in self.frameworks.items() if root in TARGETS)

    @property
    def foreign(self) -> list[tuple[str, int]]:
        """Detected frameworks this tool does not port, heaviest first."""
        return sorted(
            ((root, n) for root, n in self.frameworks.items() if root not in TARGETS),
            key=lambda kv: (-kv[1], kv[0]),
        )

    @property
    def monkeypatched(self) -> bool:
        return bool(self.patch_sites)

    @property
    def portable(self) -> bool:
        """Is there anything here this tool is built to translate?

        Monkeypatching disqualifies a tree even when FastAPI is present: the
        framework is not what breaks, the call semantics underneath it are.
        """
        return self.target_modules > 0 and not self.monkeypatched

    def headline(self) -> str:
        """One line naming the stack, for the top of a report."""
        if not self.frameworks and not self.runtimes:
            return "no web framework recognized"
        parts = [
            f"{FRAMEWORKS[root]} ({n} module{'s' if n != 1 else ''})"
            for root, n in sorted(self.frameworks.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        parts += [
            f"{RUNTIMES[root]} ({n} module{'s' if n != 1 else ''})"
            for root, n in sorted(self.runtimes.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return ", ".join(parts)

    def warnings(self) -> list[str]:
        """Everything the reader must be told before believing a coverage number.

        Ordered by how badly a silent version of it would mislead: a wrong
        concurrency model first, then a framework this tool cannot port, then a
        tree it recognized nothing in at all.
        """
        out: list[str] = []
        if self.monkeypatched:
            where = ", ".join(f"{path}:{line}" for path, line in self.patch_sites[:3])
            runtime = ", ".join(RUNTIMES[r] for r in sorted(self.runtimes)) or "a monkeypatcher"
            out.append(
                f"This tree is monkeypatched ({runtime}; {where}). Every blocking call "
                "beneath that patch is a yield point, so a mechanical rewrite to async "
                "produces code that passes its tests and serialises in production. "
                "`wreath port` cannot port this tree; the concurrency model has to be "
                "decided by a human first."
            )
        if self.foreign and not self.target_modules:
            named = ", ".join(f"{FRAMEWORKS[root]} ({n})" for root, n in self.foreign)
            out.append(
                f"This looks like {named}, and `wreath port` translates FastAPI and "
                "Starlette. Any findings below come from rules that fired on a "
                "coincidence of spelling, not on a framework this tool understands."
            )
        elif self.foreign:
            named = ", ".join(f"{FRAMEWORKS[root]} ({n})" for root, n in self.foreign)
            out.append(
                f"Mixed stack: {named} alongside the FastAPI/Starlette this tool ports. "
                "Only the latter is translated; the rest is reported as-is."
            )
        if not self.frameworks and not self.runtimes and self.modules:
            out.append(
                f"No web framework recognized across {self.modules} modules. Either this "
                "is not a web application, or it uses one this tool has never seen — "
                "in both cases the analysis below is not evidence of anything."
            )
        return out

    def as_dict(self) -> dict:
        return {
            "headline": self.headline(),
            "frameworks": {FRAMEWORKS[r]: n for r, n in self.frameworks.items()},
            "runtimes": {RUNTIMES[r]: n for r, n in self.runtimes.items()},
            "monkeypatched": self.monkeypatched,
            "patch_sites": [{"file": f, "line": ln} for f, ln in self.patch_sites],
            "portable": self.portable,
            "warnings": self.warnings(),
        }
