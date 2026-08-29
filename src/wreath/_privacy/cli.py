"""Presentation-only bindings for `wreath privacy`.

`_cli.py` is shared by every subcommand, so the implementation lives here and
only the parser registration and one dispatch clause live there --
`infra.cli`, `_migrations.cli` and `_docs.cli` set that precedent. Nothing
here decides anything: it resolves a `wreath.privacy.Privacy` object, calls it
once, and prints the rendering.

**There is deliberately no `erase` action.** This command is the *reading*
half, exactly as `wreath infra infer` is: it emits a plan for a person to
check, and running it belongs to the application that owns the database, where
the connection, the job context and the statutory clock on the request all
already exist. A CLI that could erase a subject would be a production delete
button reachable from a shell with an application target and no other context,
and the digest it would quote is the one thing this command exists to produce.

An action a person cannot undo should be issued from the place that knows why
it is being issued.
"""

from __future__ import annotations

import argparse
from typing import Any

from .._target import load_target

__all__ = ["add_privacy_parser", "execute"]


def add_privacy_parser(commands: Any) -> None:
    """Register `wreath privacy` on `_cli.build_parser`'s subparsers."""
    privacy = commands.add_parser(
        "privacy",
        help="read an application's data classification: erasure plans, access "
        "requests, retention windows",
    )
    actions = privacy.add_subparsers(dest="privacy_action", required=True)

    plan = actions.add_parser(
        "plan", help="what erasing one subject would do, and what it would miss"
    )
    plan.add_argument("target", help="a Privacy object as module:attribute")
    plan.add_argument("--subject", required=True, help="the subject's identity value")
    plan.add_argument("--format", dest="privacy_format", default="text", choices=("text", "json"))

    access = actions.add_parser(
        "access", help="the read-mode traversal behind a subject-access request"
    )
    access.add_argument("target", help="a Privacy object as module:attribute")
    access.add_argument("--subject", required=True)
    access.add_argument("--format", dest="privacy_format", default="text", choices=("text", "json"))

    retention = actions.add_parser(
        "retention", help="every declared window, and every table that lacks one"
    )
    retention.add_argument("target", help="a Privacy object as module:attribute")


def _load(spec: str) -> Any:
    """Resolve `module:attribute` to a `Privacy` instance.

    Refused by type rather than duck-typed. A target that is a module or a
    class would produce a confusing `AttributeError` three frames down, and
    the fix ("point at the Privacy object, not the module holding it") is worth
    saying once here.
    """
    target = load_target(spec, label="privacy")
    from ..privacy import Privacy

    if not isinstance(target, Privacy):
        raise ValueError(
            f"{spec} is a {type(target).__name__}, not a wreath.privacy.Privacy. "
            "Point at the Privacy object your application declared its "
            "classifications on"
        )
    return target


def execute(namespace: argparse.Namespace) -> int:
    """Run one `wreath privacy` action. Returns the process exit code.

    A blocked plan exits 1. That is the point of the command: an erasure that
    would leave the subject's data behind is a finding, and a CI step that runs
    this should fail on it rather than print it -- the same contract
    `wreath infra infer` has for a settings key nothing supplies.
    """
    privacy = _load(namespace.target)
    action = namespace.privacy_action
    if action == "retention":
        for line in privacy.retention():
            print(line)
        return 0
    fmt = getattr(namespace, "privacy_format", "text")
    if action == "access":
        print(privacy.render(privacy.access(namespace.subject), format=fmt), end="")
        return 0
    if action != "plan":  # pragma: no cover - argparse rejects first
        raise ValueError(f"unknown privacy action {action!r}")
    plan = privacy.plan(namespace.subject)
    print(privacy.render(plan, format=fmt), end="")
    return 1 if plan.blocked else 0
