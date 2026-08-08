"""Thin CLI facade for `wreath docs` — mirrors the migrations CLI.

`build` renders the site, `check` builds strictly and reports orphan/dead-link
issues with a `migrations check`-style exit code (0 clean, 1 on findings, 2 on
usage), and `serve` builds then serves the output for local preview.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import logging as _log
from . import build
from .config import Site


def _load_site(config_path: str) -> Site | None:
    path = Path(config_path)
    if not path.is_file():
        print(f"wreath docs: config not found: {config_path}", file=sys.stderr)
        return None
    spec = importlib.util.spec_from_file_location("wreath_docs_config", path)
    if spec is None or spec.loader is None:
        print(f"wreath docs: cannot load {config_path}", file=sys.stderr)
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    site = getattr(module, "site", None)
    if not isinstance(site, Site):
        print(f"wreath docs: {config_path} must define a `site = Site(...)`", file=sys.stderr)
        return None
    return site


def _report(report: Any) -> None:
    for warning in report.warnings:
        print(f"  warning: {warning}")
    for error in report.errors:
        print(f"  ERROR: {error}")


def execute(namespace: Any) -> int:
    action = getattr(namespace, "docs_action", None)
    if action not in ("build", "check", "serve"):
        print("wreath docs: expected 'build', 'check', or 'serve'.", file=sys.stderr)
        return 2

    site = _load_site(namespace.config)
    if site is None:
        return 2
    root = Path(namespace.config).resolve().parent

    if action == "check":
        report = build(replace(site, strict=True), root=root)
        _report(report)
        if report.errors:
            print(f"wreath docs check: {len(report.errors)} error(s)", file=sys.stderr)
            return 1
        print(f"wreath docs check: {report.pages} page(s) clean"
              + (f", {len(report.warnings)} warning(s)" if report.warnings else ""))
        return 0

    report = build(site, root=root)
    _report(report)
    if report.errors:
        return 1
    print(f"wreath docs: built {report.pages} page(s) into {report.output}")
    if action == "serve":
        return _serve(
            site, root, Path(report.output), getattr(namespace, "port", 8000),
            reload=not getattr(namespace, "no_reload", False),
        )
    return 0


def _sources(site: Site, root: Path, config: Path) -> list[Path]:
    """Every file a rebuild would read: the markdown tree and the config itself."""
    return [config, *sorted((root / site.source).rglob("*.md"))]


def _stamp(paths: list[Path]) -> tuple:
    """A cheap signature that changes when any watched file does."""
    marks = []
    for path in paths:
        try:
            marks.append((path.as_posix(), path.stat().st_mtime_ns))
        except OSError:
            continue                     # deleted between listing and stat
    return tuple(marks)


#: Bytes a record can spend on the path. A log cell gives **32 bytes to all of
#: its arguments together** (`_flight_schema.LOG_INLINE_ARG_BYTES`); an int
#: costs 9 of them and a string costs its UTF-8 length plus 2. So `status` (9)
#: leaves 21 bytes of path, and that is the whole budget.
#:
#: The first version of this logged method, path, status and duration. The
#: encoder packs in declaration order and stops at the first argument that will
#: not fit, so every request to a real page arrived with its status and duration
#: silently dropped behind a `truncated` flag. Measured over this site's own 241
#: output paths: with `status` and a duration packed first, the path fits whole
#: in **1%** of them; with `status` alone, 30%. The rest are clipped here rather
#: than by the encoder, which is the difference between a decision and a defect.
_PATH_BYTES = 21

#: Marks a clipped path. Three bytes in UTF-8, not one -- the first cut of this
#: budgeted in *characters*, so a 21-character path was a 23-byte payload and
#: the encoder truncated the very records the clipping existed to keep whole.
_ELLIPSIS = "\u2026"


def _short(path: str) -> str:
    """`path`, clipped from the **left** to fit one log cell's byte budget.

    From the left because a URL identifies itself at the tail: given
    `/cookbook/recipes/serve-a-grpc-method.html`, the half worth reading is
    `serve-a-grpc-method`, and clipping the other end yields a run of
    `/cookbook/recipes/` lines nobody can tell apart.

    Clipped by bytes and decoded leniently, so a multi-byte character landing on
    the boundary is dropped rather than emitted as half of itself.
    """
    raw = path.encode("utf-8")
    if len(raw) <= _PATH_BYTES:
        return path
    keep = _PATH_BYTES - len(_ELLIPSIS.encode("utf-8"))
    return _ELLIPSIS + raw[-keep:].decode("utf-8", "ignore")


#: One line per request, through `wreath.logging`'s registration tier -- the
#: same ring, projector and writer a served application uses, rather than a
#: `print` beside them. Registered at import: the template, the field names,
#: their types and their redaction are all constant, so the request path carries
#: only the two values that vary.
#:
#: `RAW` on the path because this is a local preview of a public static site.
#: The default for a string is to fingerprint it, which would render every line
#: as a hash and defeat the whole point of watching requests go by; everything
#: served here is already on the reader's disk.
#:
#: **No duration field.** It was measured before it was dropped: every static
#: file came back in `0.0ms`, because they are served from the page cache and
#: the number rounds to nothing. Nine bytes of a thirty-two byte budget for a
#: constant zero is a bad trade against nine more characters of path.
_ACCESS = _log.event(
    "docs.request",
    "{status} {path}",
    level=_log.INFO,
    fields=(_log.field("status", int), _log.field("path", str, _log.RAW)),
)


def _log_access(request: Any, response: Any) -> None:
    """Emit one access line. Never raises: a preview must not fail on its logger."""
    _ACCESS(getattr(response, "status", 0), _short(request.path))


def preview_app(directory: Path) -> Any:
    """The application `wreath docs serve` runs: one static mount at the root.

    **Wreath's own server and `wreath.staticfiles`, not `http.server`.** The
    preview used to be a `SimpleHTTPRequestHandler`, which is a strange thing
    for a site whose config module opens by calling itself the hero dogfood —
    and `_docs/site.py` has always described its own output as "a plain
    directory of self-contained HTML you can serve with wreath's hardened
    `StaticFiles`". It was simply never revisited after the generator landed.

    What the stdlib handler was costing, beyond the principle:

    * **No conditional requests.** `StaticFiles` derives an `ETag` from mtime
      and size and answers `If-None-Match` with a 304; the stdlib handler does
      not, so every reload was a full transfer of every asset and the caching
      behaviour of the real site could not be observed at all.
    * **A different containment story from the one that ships.** `StaticFiles`
      opens beneath a trusted root descriptor (`wreath._fsguard`), so the file
      checked is the file served.
    * **A `Server` header naming the Python version**, where wreath sends its
      own.

    The practical consequence was that `wreath audit` reports compression,
    cache and security-header findings about this site that a local preview had
    no way to reproduce, because the thing being previewed was not the thing
    being audited.
    """
    from ..app import Wreath
    from ..middleware.base import MiddlewareHooks

    app = Wreath()
    app.add_global_middleware(
        MiddlewareHooks(after_inplace=_log_access), priority=-90)
    # `html_index` is what makes `/guides/` reach `guides/index.html`, which is
    # the shape every directory in a docs tree has.
    app.static("/", str(directory), html_index=True)
    return app


def _serve(site: Site, root: Path, directory: Path, port: int, *, reload: bool) -> int:
    """Preview the built site, rebuilding when a source file changes.

    Polling rather than inotify: a watcher would be a dependency, and a docs
    tree is small enough that stat-ing it twice a second costs nothing. The
    browser is not told to refresh — reloading the tab is one keystroke, and a
    live-reload socket would mean shipping a server into every built page.

    The rebuild runs in a worker thread rather than on the loop. It is a
    synchronous pass over the whole tree that takes appreciably longer than a
    request, and running it inline would stall every response for its duration
    — including the reload the author is waiting on.
    """
    import asyncio

    from ..server import ServerConfig, serve
    from ..telemetry import Mode, TelemetryConfig

    # A recorder, because `wreath.logging` rides its ring: without one there is
    # no ring for a record and no projector to correlate it, so every `log.*`
    # call stays the no-op it is before a server boots. `Pulse` is the cheapest
    # mode that still creates one -- the preview wants the access line, not
    # per-phase forensics. `log_writer` is left at its default, which is text on
    # a terminal and JSON lines when the output is redirected.
    async def run() -> None:
        server = await serve(
            preview_app(directory),
            ServerConfig(host="127.0.0.1", port=port,
                         telemetry=TelemetryConfig(mode=Mode.PULSE)))
        print(f"wreath docs: serving {directory} at "
              f"http://127.0.0.1:{port} (ctrl-c to stop)")
        try:
            if reload:
                await _watch(site, root)
            else:
                await server.wait_closed()
        finally:
            # `close()` is the graceful sequence and resolves `wait_closed`
            # itself; it is idempotent, so this is safe on the path where the
            # server closed on its own.
            await server.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nwreath docs: stopped")
    return 0


async def _watch(site: Site, root: Path) -> None:
    """Rebuild whenever a watched source file changes. Never returns."""
    import asyncio

    config = Path(root / "wreath_docs.py")
    watched = _sources(site, root, config)
    print(f"wreath docs: watching {len(watched)} source file(s); "
          "edit and reload the page")
    stamp = _stamp(watched)
    while True:
        await asyncio.sleep(0.4)
        watched = _sources(site, root, config)
        current = _stamp(watched)
        if current == stamp:
            continue
        stamp = current
        fresh = await asyncio.to_thread(build, site, root=root)
        _report(fresh)
        print(f"wreath docs: rebuilt {fresh.pages} page(s)")
