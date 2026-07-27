"""Thin presentation layer for ``wreath port`` (mirrors ``_migrations_cli``).

All analysis lives in the ``_port`` core; this only parses the namespace, drives
``analyze_all``, and renders.

**Exit codes follow the same convention as the rest of the CLI**: ``2`` means the
command never got started, ``1`` means it ran and has something to report, ``0``
means it ran clean. ``wreath docs`` uses exactly this split — ``2`` for an
unknown action or a config that would not load, ``1`` for a build that ran and
had errors.

===== ==========================================================================
 0     The analysis ran and left nothing for a human. Every recognized construct
       translates, and every file was read. **An app that has already been
       ported lands here**: files analyzed, nothing recognized, nothing skipped
       is a clean run, so a regression-check re-run stays green.
 1     The analysis ran and there is work remaining: unsupported constructs,
       files that could not be read, or both. The report names which.
 2     The analysis never ran over anything -- no Python file was analyzed. In
       practice this is a wrong or empty directory. Unreadable source paths
       raise from here and reach the same code via ``CliError`` in ``_cli``.
===== ==========================================================================

Skipped files fold into ``1`` rather than earning a code of their own. They do
change what the numbers mean -- **an unsupported count taken over a partial tree
is a lower bound rather than a count** -- and the report says so in the summary
line, the ``skipped`` section, and ``files_analyzed``. But a third level would be
a scheme no other wreath command has, and the case that actually needs its own
code is "you pointed me at nothing", which ``2`` covers.

Emit mode (``--output``/``--in-place``) reads the same way: sources that could
not be read are work remaining (``1``), a tree with nothing to emit at all is
``2``, and everything written is ``0``.
"""
from __future__ import annotations

import json
from pathlib import Path

from .analyzer import analyze_all
from .emit import port_tree

#: Ran clean: nothing unsupported, nothing skipped. Includes an already-ported
#: tree, which is a successful run that happens to have nothing left to do.
EXIT_OK = 0
#: Ran, and left work: unsupported constructs, unreadable files, or both.
EXIT_WORK_REMAINS = 1
#: Never ran over anything -- no Python file was analyzed. A wrong or empty
#: directory, and the same code ``_cli`` raises for a source path that is absent.
EXIT_NOT_RUN = 2


def execute(namespace) -> int:
    roots = [Path(s) for s in namespace.source]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        raise ValueError(f"source path(s) not found: {', '.join(missing)}")

    in_place = bool(getattr(namespace, "in_place", False))
    output = getattr(namespace, "output", None)
    force = bool(getattr(namespace, "force", False))

    # Emit mode (Phase 1): --output <dir> or --in-place. Otherwise report-only.
    if in_place or output:
        total = 0
        touched = 0
        failed = []
        for root in roots:
            result = port_tree(root, output, in_place=in_place, force=force)
            total += len(result.written_files) + len(result.regenerated)
            touched += (len(result.written_files) + len(result.regenerated)
                        + len(result.skipped) + len(result.failed))
            failed.extend(result.failed)
            for path in result.written_files:
                print(f"wrote      {path}")
            for path in result.regenerated:
                print(f"regenerated {path}")
            for path in result.skipped:
                print(f"skipped    {path}")
            for item in result.failed:
                print(f"FAILED     {item.file} — {item.reason}: {item.detail}")
        print(f"\n{total} file(s) emitted. Review every `# TODO(wreath-port: ...)` before use.")
        if failed:
            noun = "file" if len(failed) == 1 else "files"
            print(f"{len(failed)} {noun} could not be read and were not ported.")
            return EXIT_WORK_REMAINS
        if not touched:
            print("No Python files were found. Check the source path.")
            return EXIT_NOT_RUN
        return EXIT_OK

    report = analyze_all(roots)
    if getattr(namespace, "as_json", False):
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.to_markdown())

    # Nothing analyzed is the one case that is about the *run* rather than the
    # code: no Python file was read, so there is no report to have an opinion
    # about. Recognizing nothing across files that were read is different — that
    # is an already-ported tree, and it is clean.
    if not report.files_analyzed:
        return EXIT_NOT_RUN
    if report.counts()["unsupported"] or report.skipped:
        return EXIT_WORK_REMAINS
    return EXIT_OK
