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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .._target import load_target

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
        "--factory",
        action="store_true",
        help="invoke the target as a zero-argument application factory",
    )
    inferred.add_argument(
        "--settings",
        action="append",
        default=[],
        metavar="SPEC",
        help="a settings dataclass whose environment contract to check, as "
        "module:Class or module:Class=PREFIX (repeatable)",
    )
    inferred.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="PATH",
        help="a dotenv file that supplies environment keys (repeatable)",
    )
    inferred.add_argument(
        "--environ",
        action="store_true",
        help="also treat this process's own environment as a supplier",
    )
    inferred.add_argument(
        "--format",
        dest="infra_format",
        default="text",
        choices=("text", "json"),
        help="text (default) or json",
    )
    bundle = actions.add_parser(
        "bundle",
        help="render an inspectable Compose bundle from a gap-free inferred plan",
    )
    bundle.add_argument("target", help="application target as module:attribute")
    bundle.add_argument("--image", required=True, help="immutable OCI image@sha256:digest")
    bundle.add_argument("--output", required=True, metavar="DIRECTORY")
    bundle.add_argument("--service", default="wreath-app")
    bundle.add_argument("--port", type=int, default=8000)
    bundle.add_argument("--factory", action="store_true")
    bundle.add_argument("--force", action="store_true")
    bundle.add_argument(
        "--settings",
        action="append",
        default=[],
        metavar="SPEC",
        help="settings dataclass as module:Class or module:Class=PREFIX (repeatable)",
    )
    bundle.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="PATH",
        help="dotenv supplier checked against the settings contract (repeatable)",
    )
    bundle.add_argument(
        "--environ",
        action="store_true",
        help="also treat this process's environment as a supplier",
    )


def _settings_models(specs: list[str]) -> list[tuple[type, str, str]]:
    """Resolve every `--settings` spec into `(dataclass, label, prefix)`.

    The prefix defaults to the empty string rather than being guessed from the
    module name. A guessed prefix would produce a plausible key for every field
    and a correct one for none, which is worse than asking.
    """
    models: list[tuple[type, str, str]] = []
    for spec in specs:
        target, _, prefix = spec.partition("=")
        model = load_target(target, label="settings")
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

    if namespace.infra_action not in {"infer", "bundle"}:
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
    if namespace.infra_action == "bundle":
        from . import deployment_bundle

        if plan.gaps:
            print(render_text(plan), end="")
            return 1
        bundle = deployment_bundle(
            plan,
            image=namespace.image,
            service=namespace.service,
            port=namespace.port,
            factory=namespace.factory,
        )
        written = bundle.write(namespace.output, force=namespace.force)
        for path in written:
            print(f"wrote {path}")
        return 0
    render = render_json if namespace.infra_format == "json" else render_text
    print(render(plan), end="")
    return 1 if plan.gaps else 0
