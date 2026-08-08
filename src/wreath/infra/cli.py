"""Presentation-only bindings for `wreath infra`.

`_cli.py` is shared by every subcommand, so the implementation lives here and
only the parser registration and one dispatch clause live there --
`_migrations.cli` and `_docs.cli` set that precedent. Nothing in this module
decides anything: it resolves the arguments into the objects
`wreath.infra.infer` takes, calls it once, and prints the rendering.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["add_infra_parser", "execute"]


def add_infra_parser(commands: Any) -> None:
    """Register `wreath infra` on `_cli.build_parser`'s subparsers."""
    infra = commands.add_parser(
        "infra",
        help="read an application and report the infrastructure it requires",
    )
    actions = infra.add_subparsers(dest="infra_action", required=True)
    inferred = actions.add_parser(
        "infer",
        help="derive a read-only plan from an application's own declarations",
    )
    inferred.add_argument("target", help="application target as module:attribute")
    inferred.add_argument(
        "--factory", action="store_true",
        help="invoke the target as a zero-argument application factory",
    )
    inferred.add_argument(
        "--settings", action="append", default=[], metavar="SPEC",
        help="a settings dataclass whose environment contract to check, as "
             "module:Class or module:Class=PREFIX (repeatable)",
    )
    inferred.add_argument(
        "--env", action="append", default=[], metavar="PATH",
        help="a dotenv file that supplies environment keys (repeatable)",
    )
    inferred.add_argument(
        "--environ", action="store_true",
        help="also treat this process's own environment as a supplier",
    )
    inferred.add_argument(
        "--format", dest="infra_format", default="text", choices=("text", "json"),
        help="text (default) or json",
    )


def _split(spec: str, *, what: str) -> tuple[str, str]:
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ValueError(f"{what} {spec!r} must be spelled module:attribute")
    return module, attribute


def _settings_models(specs: list[str]) -> list[tuple[type, str, str]]:
    """Resolve every `--settings` spec into `(dataclass, label, prefix)`.

    The prefix defaults to the empty string rather than being guessed from the
    module name. A guessed prefix would produce a plausible key for every field
    and a correct one for none, which is worse than asking.
    """
    models: list[tuple[type, str, str]] = []
    for spec in specs:
        target, _, prefix = spec.partition("=")
        module_name, attribute = _split(target, what="settings model")
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            raise ValueError(
                f"could not import settings module {module_name!r}: {error}"
            ) from error
        try:
            model = getattr(module, attribute)
        except AttributeError as error:
            raise ValueError(
                f"settings module {module_name!r} has no attribute {attribute!r}"
            ) from error
        if not (dataclasses.is_dataclass(model) and isinstance(model, type)):
            raise ValueError(
                f"settings target {target!r} is not a dataclass type; "
                "Environment.bind takes a dataclass"
            )
        models.append((model, target, prefix))
    return models


def _suppliers(paths: list[str], *, environ: bool) -> tuple[dict[str, str], dict[str, str]]:
    """`(every supplier, the dotenv subset)`, later files winning over earlier ones.

    The process environment is kept out of the second mapping deliberately. An
    unread-key report is worth having for a file somebody authored to configure
    this application, and worthless for the several hundred unrelated keys a
    shell carries.
    """
    from ..config import parse_dotenv, read_osenv

    supplied: dict[str, str] = {}
    dotenv: dict[str, str] = {}
    for raw in paths:
        path = Path(raw)
        try:
            values = parse_dotenv(path.read_bytes())
        except OSError as error:
            raise ValueError(f"could not read dotenv {raw}: {error}") from error
        except ValueError as error:
            # The dialect is strict on purpose -- a comment, an `export`, and a
            # line without an `=` are all refused -- and the refusal names the
            # line but not the file, which is no use when several were given.
            raise ValueError(f"{raw}: {error}") from error
        for key in values:
            supplied[key] = raw
            dotenv[key] = raw
    if environ:
        for key in read_osenv():
            supplied.setdefault(key, "process")
    return supplied, dotenv


def execute(
    namespace: argparse.Namespace,
    load_application: Callable[..., Any],
) -> int:
    """Run one `wreath infra` action. Returns the process exit code.

    A plan with gaps exits 1. That is the point of the command: a settings key
    nothing supplies is a deployment that will start and die, and a CI step that
    runs this should fail on it rather than print it.
    """
    from . import infer, render_json, render_text

    if namespace.infra_action != "infer":  # pragma: no cover - argparse rejects first
        raise ValueError(f"unknown infra action {namespace.infra_action!r}")
    models = _settings_models(namespace.settings)
    supplied, dotenv = _suppliers(namespace.env, environ=namespace.environ)
    app = load_application(namespace.target, factory=namespace.factory)
    plan = infer(
        app,
        application=namespace.target,
        settings=models,
        supplied=supplied,
        dotenv_keys=dotenv,
    )
    render = render_json if namespace.infra_format == "json" else render_text
    print(render(plan), end="")
    return 1 if plan.gaps else 0
