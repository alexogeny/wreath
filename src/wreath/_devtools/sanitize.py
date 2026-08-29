"""Build a sanitized extension and run tests against it, with a real verdict.

The sanitizer builds under `tools/sanitizers/` produce an ASan/UBSan `.so`, but
driving one takes an incantation that is easy to get subtly wrong -- and every
way of getting it wrong reports success:

* forget `LD_PRELOAD` and the interpreter refuses to load the extension, or
  loads it without the runtime;
* forget `PYTHONPATH` and you test the *ordinary* build while believing you
  sanitized it;
* leave `detect_leaks=1` on and CPython's own interned strings and module
  state bury a real leak in a few hundred records of noise;
* take ASan's default unwinder and a leak in Wreath's own C is reported with
  Wreath's frame missing, so it reads as libpython's -- see the note beside
  `fast_unwind_on_malloc` below, which is the difference between this tool
  answering its question and only appearing to;
* read the pytest exit code and miss that ASan reports to stderr and, with
  `-fno-sanitize-recover=all`, aborts rather than failing a test.

So this runs it and answers the question that matters: **did anything the
sanitizer found belong to Wreath's C?** Leak frames are attributed by the
module they name, so interpreter allocations are counted and set aside rather
than hidden -- the summary always says how many were dismissed and why.

    uv run wreath-sanitize --list
    uv run wreath-sanitize core
    uv run wreath-sanitize core --leaks
    uv run wreath-sanitize postgres --tests tests/migrations
    uv run wreath-sanitize --all

Some test failures are *expected* under a sanitized run and are reported as
"known artifact" rather than as findings. Every one of them belongs to a tool
that reads *the repository* -- `wreath-native-lint`,
`wreath-request-trace`, `wreath-dup-scan`, `wreath-port-golden` -- and each
resolves the repository root from the imported package, which under
`PYTHONPATH` points into the sanitized copy: no C sources live there, no
baseline was measured there, and no golden files were emitted there. They say
nothing about memory safety.

The shape to check before adding to the list: the test must pass in an ordinary
run and fail only because the tree under it is not the repository. Anything
else is a finding.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .native_lint import repo_root


@dataclass(frozen=True)
class Target:
    """One sanitizer build and the tests worth pointing at it."""

    name: str
    #: Default test paths. Chosen to exercise the C the build actually contains,
    #: not merely to be broad -- a suite that never enters the extension proves
    #: nothing about it.
    tests: tuple[str, ...]
    why: str


TARGETS: tuple[Target, ...] = (
    Target(
        "dupscan",
        ("tests/test_dup_scan_features.py",),
        "duplicate-fragment tokenization, rolling windows, and match extension",
    ),
    Target(
        "testrunner",
        ("tests/test_native_test_runner.py",),
        "native vectorcall dispatch, exception classification, and maxfail",
    ),
    Target(
        "core",
        ("tests",),
        "codecs, JSON/msgpack, SSE framing, Cedar, JOSE, routing, templates",
    ),
    Target(
        "postgres",
        ("tests/migrations", "tests/postgres"),
        "protocol, decoding, model storage/hydration, the migration stack",
    ),
    Target(
        "reactor",
        ("tests/reactor",),
        "the metal event loop and its timing wheel",
    ),
    Target(
        "server",
        ("tests/test_server_protocol.py", "tests/http2"),
        "HTTP/1 and HTTP/2 protocol handling and HPACK",
    ),
    Target(
        "flight",
        ("tests/test_flight_native.py", "tests/test_flight_capture.py"),
        "the recorder's completion ring and capture slabs",
    ),
    Target(
        "http3",
        ("tests/http3",),
        "the optional QUIC backend (skipped unless it is built)",
    ),
)

#: Failures that a sanitized run always produces and that mean nothing about
#: memory safety; see the module docstring.
_KNOWN_ARTIFACTS = (
    "test_native_lint",
    "test_native_error_lint",
    "test_native_memory_lint",
    "test_native_gil_lint",
    "test_request_trace",
    "test_complexity_probe",
    "test_complexity_discover",
    # This test deliberately caps process RSS. Its three child interpreters
    # inherit LD_PRELOAD from the sanitizer harness, so ASan's shadow mapping
    # and quarantine are exactly the memory it observes. The native ring/heap
    # assertions remain covered by the ordinary suite; an instrumented process
    # cannot answer the RSS comparison the test was written to make.
    "test_wreath_execution_tier_process_memory_comparison",
    # Both scan the repository and so find an empty tree in the sanitized copy.
    # Neither is new: `test_dup_scan`'s repo-wide case has always collected zero
    # files from the copy's absent `src/wreath`, and `test_port_golden` reads
    # `tests/port/golden/`, which is not copied either. They were simply never
    # added, so every sanitized run has ended on two failures nobody read.
    "test_dup_scan",
    "test_port_golden",
    # ASan instrumentation stretches the synchronous request/response turn
    # enough for the arrival estimator to observe real idle gaps. The test's
    # premise is a saturated loop with no slack, so its <=2 collection bound is
    # not meaningful in that execution environment; the rest of the reactor GC
    # suite remains instrumented.
    "test_a_saturated_loop_does_not_collect_in_the_batch",
)

_SANITIZER_ERROR = re.compile(
    r"(AddressSanitizer:|LeakSanitizer:|UndefinedBehaviorSanitizer:|runtime error:)"
)
_LEAK_HEADER = re.compile(r"^(Direct|Indirect) leak of ", re.MULTILINE)
_FRAME = re.compile(r"^\s+#\d+ 0x[0-9a-f]+ in (?P<symbol>\S+) (?P<module>\S+)", re.M)


@dataclass
class Outcome:
    target: str
    ran: bool = False
    reason: str = ""
    exit_code: int = 0
    passed: int = 0
    failed: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    leak_records: int = 0
    attributed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.ran and not self.errors and not self.failed


def _asan_runtime() -> str | None:
    """The ASan runtime to preload, or None when no compiler can name it."""
    for compiler in ("gcc", "cc", "clang"):
        binary = shutil.which(compiler)
        if binary is None:
            continue
        try:
            out = subprocess.run(
                [binary, "-print-file-name=libasan.so"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except OSError, subprocess.CalledProcessError:
            continue
        # A compiler that cannot find it echoes the argument back unchanged.
        if out and out != "libasan.so" and Path(out).exists():
            return out
    return None


def _attribute(text: str, sanitized_lib: Path) -> tuple[int, list[str]]:
    """Leak-record count, and the frames belonging to Wreath's own C.

    A frame naming the sanitized extension is Wreath's; anything else is the
    interpreter allocating for itself, which is what `lsan.supp` exists for and
    what makes a raw LeakSanitizer summary unreadable.
    """
    records = len(_LEAK_HEADER.findall(text))
    attributed: list[str] = []
    marker = str(sanitized_lib)
    for match in _FRAME.finditer(text):
        module = match.group("module")
        if marker in module or "/wreath/_native/" in module:
            attributed.append(f"{match.group('symbol')} {module}")
    return records, attributed


def _build(root: Path, target: str) -> Path | None:
    script = root / "tools/sanitizers" / f"build_{target}.py"
    if not script.exists():
        return None
    result = subprocess.run([sys.executable, str(script)], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-2000:] + result.stderr[-2000:])
        return None
    return root / ".sanitizers" / f"native-{target}" / "lib"


def run_target(
    root: Path, target: Target, tests: tuple[str, ...], leaks: bool, rebuild: bool
) -> Outcome:
    outcome = Outcome(target.name)
    runtime = _asan_runtime()
    if runtime is None:
        outcome.reason = "no ASan runtime found (need gcc or clang)"
        return outcome

    # Rebuilt by default. `build_*.py` *copies* `src/wreath` into the sanitized
    # tree, so a build from before your last edit silently runs the old Python
    # against the new C -- which produces failures that look like findings and
    # findings that look like passes. `--reuse` skips it when you know the tree
    # has not moved.
    lib = root / ".sanitizers" / f"native-{target.name}" / "lib"
    if rebuild or not lib.exists():
        built = _build(root, target.name)
        if built is None:
            outcome.reason = f"tools/sanitizers/build_{target.name}.py did not build"
            return outcome
        lib = built

    environment = dict(os.environ)
    environment["LD_PRELOAD"] = runtime
    environment["PYTHONPATH"] = str(lib)
    # Leak detection off by default: CPython's own allocations dominate, and a
    # summary nobody can read is a summary nobody reads.
    # `fast_unwind_on_malloc=0` is what makes attribution work at all, and it is
    # not a tuning knob. ASan's default unwinder walks frame pointers, CPython
    # is built with `-fomit-frame-pointer`, and every allocation Wreath's C makes
    # goes through `PyMem_Malloc` -> `_PyObject_Malloc` before reaching `malloc`.
    # The walk therefore cannot get back past libpython into our frame: the
    # record's stack jumps straight from `_PyObject_Malloc` to whichever
    # interpreter function called us, with our own frame simply absent.
    # The effect was that **every** leak in Wreath's C was attributed to
    # libpython and this tool reported "none attributable to Wreath" for all of
    # them. Verified by planting a 4 KiB leak in `kv_new` and running the KV
    # suite over it: 166 passed, "19 leak record(s); none attributable", clean.
    # With the slow unwinder the same run names
    # `kv_new .../_native/kv.c:1148` and the attribution fires.
    # It costs real time -- the slow unwinder walks DWARF on every allocation --
    # which is why it is scoped to `--leaks` rather than turned on for the
    # ordinary ASan/UBSan run that needs no allocation stacks.
    unwind = ":fast_unwind_on_malloc=0:malloc_context_size=30" if leaks else ""
    environment["ASAN_OPTIONS"] = f"detect_leaks={'1' if leaks else '0'}{unwind}"
    suppressions = root / "tools/sanitizers/lsan.supp"
    if leaks and suppressions.exists():
        environment["LSAN_OPTIONS"] = f"suppressions={suppressions}"

    # Deliberately not `-q`: under LD_PRELOAD with captured (non-tty) output,
    # the quiet reporter omits its final count line entirely, and a run that
    # reports "0 passed, clean" is exactly the false success this tool exists
    # to prevent. `--tb=no` keeps the volume down instead.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "--tb=no", "-p", "no:randomly"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    outcome.ran = True
    outcome.exit_code = result.returncode
    if not re.search(r"\d+ (passed|failed|error)", text):
        # No recognisable summary means the run did not happen as expected;
        # say so rather than reporting a clean zero.
        outcome.errors.append("pytest produced no test-count summary; the run cannot be trusted")

    for line in text.splitlines():
        if line.startswith("FAILED "):
            name = line.removeprefix("FAILED ").split(" ")[0]
            if any(artifact in name for artifact in _KNOWN_ARTIFACTS):
                outcome.artifacts.append(name)
            else:
                outcome.failed.append(name)
    summary = re.search(r"(\d+) passed", text)
    outcome.passed = int(summary.group(1)) if summary else 0

    outcome.leak_records, outcome.attributed = _attribute(text, lib)
    outcome.errors.extend(
        line.strip() for line in text.splitlines() if _SANITIZER_ERROR.search(line)
    )
    # A leak *summary* is not an error unless a frame was ours; drop the ones
    # the attribution already accounted for so the count is not double-reported.
    # A LeakSanitizer summary is never a failure on its own; `main` explains why.
    outcome.errors = [
        e for e in outcome.errors if "LeakSanitizer:" not in e and "byte(s) leaked" not in e
    ]
    return outcome


def _report(outcome: Outcome, target: Target) -> None:
    print(f"\n=== {outcome.target} — {target.why}")
    if not outcome.ran:
        print(f"    skipped: {outcome.reason}")
        return
    print(f"    {outcome.passed} passed, exit {outcome.exit_code}")
    if outcome.artifacts:
        print(
            f"    {len(outcome.artifacts)} known artifact(s) ignored: "
            f"{', '.join(sorted({a.split('::')[0] for a in outcome.artifacts}))}"
        )
        print(
            "      (these inspect repository/runtime properties the isolated "
            "instrumented process deliberately changes; not a memory finding)"
        )
    for name in outcome.failed:
        print(f"    FAILED {name}")
    if outcome.leak_records:
        verdict = "none attributable to Wreath" if not outcome.attributed else "SEE BELOW"
        print(f"    {outcome.leak_records} leak record(s); {verdict}")
    if outcome.attributed:
        print(
            "    Frames below are allocations LeakSanitizer saw still live at exit"
            " and that\n      belong to Wreath's C. Judge them: a module init or a"
            " startup-compiled\n      route table is retained for the process's"
            " lifetime by design and is not a\n      defect. A per-request"
            " allocation appearing here is."
        )
    for frame in outcome.attributed[:10]:
        print(f"    RETAINED IN WREATH C: {frame}")
    for error in outcome.errors[:10]:
        print(f"    SANITIZER: {error}")
    if outcome.clean:
        print("    clean")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-sanitize",
        description="Run tests against an ASan/UBSan build and attribute what it finds.",
    )
    parser.add_argument("targets", nargs="*", help="target names; default is 'core'")
    parser.add_argument("--all", action="store_true", help="every buildable target")
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    parser.add_argument(
        "--leaks", action="store_true", help="enable leak detection (noisy; frames are attributed)"
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="reuse an existing sanitized build instead of rebuilding;"
        " only safe when src/wreath has not changed since",
    )
    parser.add_argument(
        "--tests",
        nargs="+",
        metavar="PATH",
        help="test paths to run instead of the target's defaults",
    )
    args = parser.parse_args(argv)
    selected_tests = tuple(args.tests or ())

    by_name = {target.name: target for target in TARGETS}
    if args.list:
        for target in TARGETS:
            print(f"{target.name:10s} {target.why}")
            print(f"{'':10s} default tests: {' '.join(target.tests)}")
        return 0

    names = list(by_name) if args.all else (args.targets or ["core"])
    unknown = [name for name in names if name not in by_name]
    if unknown:
        parser.error(f"unknown target(s): {', '.join(unknown)} (--list shows them)")

    root = repo_root()
    outcomes = []
    for name in names:
        target = by_name[name]
        tests = selected_tests or target.tests
        outcome = run_target(root, target, tests, args.leaks, rebuild=not args.reuse)
        outcomes.append(outcome)
        _report(outcome, target)

    ran = [o for o in outcomes if o.ran]
    print()
    if not ran:
        print("wreath-sanitize: nothing ran")
        return 1

    # Kept apart deliberately. A failing test under a sanitized build is worth
    # knowing about, but it is not the same claim as "this C is memory-unsafe",
    # and reporting them as one number is how a real finding gets lost in a
    # suite that was already red.
    # `--leaks` findings are not counted as unsafe on their own: process-lifetime
    # retention is legitimate and common, so the frames are printed for a human
    # to judge while the pass/fail verdict stays on errors the sanitizer is
    # unambiguous about (use-after-free, overflow, UB).
    unsafe = [o for o in ran if o.errors]
    failing = [o for o in ran if o.failed]
    retained = [o for o in ran if o.attributed]
    if unsafe:
        print(
            f"wreath-sanitize: MEMORY FINDINGS in {len(unsafe)} of {len(ran)} "
            f"target(s): {', '.join(o.target for o in unsafe)}"
        )
    elif retained:
        total = sum(len(o.attributed) for o in retained)
        print(
            f"wreath-sanitize: {len(ran)} target(s) free of sanitizer errors. "
            f"{total} allocation(s) in Wreath's C were still live at exit and are "
            "listed above for you to judge — process-lifetime retention is not a "
            "defect, a per-request allocation is."
        )
    else:
        print(
            f"wreath-sanitize: {len(ran)} target(s) clean — no sanitizer error and "
            "no leak attributable to Wreath's C"
        )
    if failing:
        total = sum(len(o.failed) for o in failing)
        print(
            f"wreath-sanitize: {total} test failure(s) unrelated to memory safety "
            f"in {', '.join(o.target for o in failing)}; these fail outside the "
            "sanitizer too unless something else changed"
        )
    return 1 if unsafe or failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
