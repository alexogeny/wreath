"""Two things that must never reach `main`: borrowed credit, and debris.

**Runnable with nothing installed.** This module imports only the standard
library and nothing from `wreath`, so CI executes it as
`python src/wreath/_devtools/hygiene.py` behind `actions/setup-python` -- no
`uv sync`, no compiled extension, about ten seconds. That constraint is the
reason it does not use `_devtools`' shared `repo_root`; a gate that costs a
27-second build is a gate people move to the end of the pipeline.

Findings:

* `HYG001` -- a co-authorship trailer. Every one of them, not a list of vendors.
  Attribution is a claim about who is answerable for a change, and the answer is
  the person who opened the pull request. A model is a tool, like an editor or a
  compiler, and neither of those signs your work either. `main`'s ruleset refuses
  the push outright; this exists to say *why*, because a rule that rejects
  without explaining gets worked around.
* `HYG002` -- a generation notice in a commit message ("Generated with ...",
  the robot emoji). Same argument, different spelling.
* `HYG003` -- the same, in a pull request's title or body.
* `HYG010` -- a path that should never be committed: agent scratch, editor and
  merge debris, caches, and `.env`.

`HYG010` deliberately overlaps `.gitignore`, which already excludes `.pi/` and
`.claude/`. Overlap is the point: `.gitignore` is advice that `git add -f`
ignores and that a new junk directory nobody thought to list never had. This
looks at what is *actually tracked*, which is the only question that matters.

None of this is about disliking the tools. Use whatever you like -- the issue
and pull request templates say so in as many words. What it refuses is a commit
that credits a tool as though the tool were accountable for the result.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Finding", "check_message", "check_paths", "main", "scan_commits"]


@dataclass(frozen=True)
class Finding:
    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.code} {self.message}"


# --- attribution --------------------------------------------------------------

#: Trailers that assign credit to somebody other than the author. Matched only
#: at the start of a line, which is what makes them trailers -- prose that
#: happens to discuss co-authorship is not a claim of it, and a linter that
#: cannot tell the difference is one people learn to route around.
_TRAILER = re.compile(
    r"^[ \t]*(co-authored-by|assisted-by|co-developed-by|generated-by|"
    r"co-created-by)[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)

#: Generation notices. `\W*` after the emoji so "🤖 Generated with" matches
#: whatever spacing or punctuation a harness put between them.
_GENERATED = re.compile(
    r"(generated|created|written|authored)\s+(with|by)\s+"
    r"[`\"']?(claude|chatgpt|gpt|copilot|cursor|gemini|llama|devin|aider|codex|"
    r"windsurf|anthropic|openai|an?\s+(ai|llm|language model))",
    re.IGNORECASE,
)
_ROBOT = re.compile(r"🤖\W*(generated|created|made|built|with|by)", re.IGNORECASE)

#: A bare robot emoji anywhere in a trailer block is the same claim without the
#: sentence, and it is what most harnesses actually emit.
_ROBOT_BARE = re.compile(r"🤖")


def check_message(text: str, *, where: str, code_prefix: str = "HYG") -> list[Finding]:
    """Attribution findings for one commit message, or one PR title plus body."""
    findings: list[Finding] = []
    trailer = _TRAILER.search(text)
    if trailer:
        line = text[: trailer.start()].count("\n") + 1
        findings.append(Finding(
            f"{code_prefix}001", f"{where}:{line}",
            f"co-authorship trailer {trailer.group(1).lower()!r} — you own this "
            "change; remove the trailer",
        ))
    for pattern, why in (
        (_GENERATED, "generation notice"),
        (_ROBOT, "generation notice"),
        (_ROBOT_BARE, "robot emoji, which reads as a generation notice"),
    ):
        match = pattern.search(text)
        if match:
            line = text[: match.start()].count("\n") + 1
            findings.append(Finding(
                f"{code_prefix}002", f"{where}:{line}",
                f"{why}: {match.group(0)!r} — describe the change, not what wrote it",
            ))
            break  # one finding per message; three spellings of it is noise
    return findings


def scan_commits(messages: dict[str, str]) -> list[Finding]:
    """Attribution findings across `{sha: message}`."""
    return [f for sha, text in messages.items()
            for f in check_message(text, where=f"commit {sha[:8]}")]


# --- stray paths --------------------------------------------------------------

#: Each entry is (regex, why). Anchored on a path segment so `scratch/` matches
#: `scratch/x` and `a/scratch/x` but not `scratchpad.py`.
_STRAY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|/)\.pi(/|$)"), "agent scratch directory"),
    (re.compile(r"(^|/)\.claude(/|$)"), "agent state; skills live in ./skills/"),
    (re.compile(r"(^|/)\.(env|envrc)(\.|$)"), "environment file — may carry secrets"),
    (re.compile(r"(^|/)(scratch|scratches|scratchpad|tmp|temp)(/|$)"), "scratch directory"),
    (re.compile(r"\.(orig|rej)$"), "merge or patch debris"),
    (re.compile(r"\.(swp|swo|swn)$|~$"), "editor swap or backup file"),
    (re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini)$"), "OS metadata"),
    (re.compile(r"(^|/)(nohup\.out|core\.\d+)$"), "process debris"),
    (re.compile(r"(^|/)__pycache__(/|$)|\.py[co]$"), "compiled Python"),
    (re.compile(r"(^|/)\.(pytest|ruff|mypy|ty)_cache(/|$)"), "tool cache"),
    (re.compile(r"(^|/)\.coverage(\.|$)|(^|/)htmlcov(/|$)"), "coverage artefact"),
)

#: `.env.example` is the committed template and the scaffold generates one, so
#: it is named rather than reached by making the `.env` pattern cleverer.
_STRAY_ALLOW = re.compile(r"(^|/)\.env\.example$")


def check_paths(paths: list[str]) -> list[Finding]:
    """`HYG010` for every path that should never have been committed."""
    findings = []
    for path in paths:
        if _STRAY_ALLOW.search(path):
            continue
        for pattern, why in _STRAY:
            if pattern.search(path):
                findings.append(Finding("HYG010", path, f"{why} — should not be committed"))
                break
    return findings


# --- git ----------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    ).stdout


def tracked_paths(root: Path) -> list[str]:
    return [line for line in _git(root, "ls-files").splitlines() if line]


def commit_messages(root: Path, revision_range: str) -> dict[str, str]:
    """`{sha: full message}` for a range, split on a marker rather than a blank line.

    `%x00` because a commit message contains blank lines by construction, and
    splitting on those merges two commits into one and reports the wrong sha.
    """
    raw = _git(root, "log", "--format=%H%x1f%B%x00", revision_range)
    messages = {}
    for raw_record in raw.split("\x00"):
        record = raw_record.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition("\x1f")
        messages[sha] = message
    return messages


def _repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-hygiene",
        description="Refuse borrowed credit and committed debris.",
    )
    parser.add_argument("--range", dest="revision_range", metavar="A..B",
                        help="check the commit messages in this range")
    parser.add_argument("--message-file", metavar="PATH",
                        help="check this file as one message (a PR title and body)")
    parser.add_argument("--paths", action="store_true",
                        help="check every tracked path (the default when no "
                             "other selector is given)")
    parser.add_argument("--root", default=None, help="repository root")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else _repo_root(Path(__file__).resolve())
    findings: list[Finding] = []

    selected = args.revision_range or args.message_file or args.paths
    if args.paths or not selected:
        findings += check_paths(tracked_paths(root))
    if args.revision_range:
        findings += scan_commits(commit_messages(root, args.revision_range))
    if args.message_file:
        text = Path(args.message_file).read_text(encoding="utf-8")
        findings += check_message(text, where="pull request")
        # A PR body carries HYG003 rather than 001/002, because the remedy is
        # editing a description rather than rewriting history.
        findings = [Finding("HYG003", f.where, f.message)
                    if f.where.startswith("pull request") else f for f in findings]

    if args.format == "json":
        print(json.dumps([{"code": f.code, "where": f.where, "message": f.message}
                          for f in findings], indent=2))
    else:
        for finding in findings:
            print(finding.render())
        print(f"\nwreath-hygiene: {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
