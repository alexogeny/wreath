"""Tests for the static superlinear sweep.

A scanner that reports nothing is indistinguishable from a clean tree, which is
the exact failure `AGENTS.md` names. So every rule here is proved twice: once
against a **planted** defect it must report, and once against a **control** that
is the linear way to write the same thing and must stay silent. The controls are
the half that matters -- without them the scanner could report everything and
still look correct.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from wreath._devtools.complexity_discover import (
    Finding,
    discover,
    infer_kinds,
    scan_c,
    scan_python,
)

# Each entry: rule code, the planted body that must fire it, and the linear
# control that must not.
PLANTED = {
    "SL-IN-LOOP": """
        def f(items, haystack):
            out = []
            for x in items:
                if x in haystack:
                    out.append(x)
            return out
    """,
    "SL-FRONT-MUTATE": """
        def f(queue):
            out = []
            while queue:
                out.append(queue.pop(0))
            return out
    """,
    "SL-ACCUM-ADD": """
        def f(parts):
            s = ""
            for p in parts:
                s += p
            return s
    """,
    "SL-SLICE-LOOP": """
        def f(buf, n):
            for _ in range(n):
                head = buf[0:4]
            return head
    """,
    "SL-LINEAR-METHOD": """
        def f(items, seq):
            return [seq.index(x) for x in items]
    """,
    "SL-LINEAR-CALL": """
        def f(items, seq):
            for _ in items:
                sorted(seq)
    """,
    "SL-NEST-SAME": """
        def f(rows):
            for a in rows:
                for b in rows:
                    yield a, b
    """,
    "SL-RECOMPILE": """
        import re
        def f(items):
            for x in items:
                re.compile(x)
    """,
    "SL-COMP-LOOP": """
        def f(items, other):
            for _ in items:
                z = [y for y in other]
            return z
    """,
    "SL-RECURSE": """
        def fib(n):
            if n < 2:
                return n
            return fib(n - 1) + fib(n - 2)
    """,
}

CONTROLS = {
    "hoisted-set-membership": """
        def g(items, haystack):
            hs = set(haystack)
            return [x for x in items if x in hs]
    """,
    "set-annotated-parameter": """
        def g(members: set[type], rows: list[str]):
            for r in rows:
                if r in members:
                    yield r
    """,
    "mapping-annotated-parameter": """
        def g(index: dict[str, int], rows: list[str]):
            for r in rows:
                if r in index:
                    yield index[r]
    """,
    "annotated-local-set": """
        def g(rows: list[str]):
            seen: set[str] = set()
            for r in rows:
                if r not in seen:
                    seen.add(r)
            return seen
    """,
    "deque-popleft": """
        from collections import deque
        def g(queue):
            q = deque(queue)
            out = []
            while q:
                out.append(q.popleft())
            return out
    """,
    "join-not-concat": """
        def g(parts):
            return "".join(parts)
    """,
    "list-augmented-assign": """
        def g(rows: list[str]):
            out: list[str] = []
            for r in rows:
                out += [r]
            return out
    """,
    "distinct-collections": """
        def g(rows, cols):
            for a in rows:
                for b in cols:
                    yield a, b
    """,
    "loop-local-slice": """
        def g(items):
            for x in items:
                head = x[0:4]
            return head
    """,
    "precompiled-pattern": """
        import re
        _RX = re.compile("x")
        def g(items):
            for x in items:
                _RX.match(x)
    """,
}


def _scan_source(tmp_path, source: str) -> list[Finding]:
    module = tmp_path / "subject.py"
    module.write_text(textwrap.dedent(source), encoding="utf-8")
    return scan_python(module, tmp_path)


@pytest.mark.parametrize("code", sorted(PLANTED))
def test_planted_defect_is_reported(tmp_path, code: str) -> None:
    """Every rule fires on the shape it exists to find."""
    findings = _scan_source(tmp_path, PLANTED[code])
    assert code in {f.code for f in findings}, (
        f"{code} did not fire on its own planted defect; "
        f"got {sorted({f.code for f in findings})}")


@pytest.mark.parametrize("name", sorted(CONTROLS))
def test_linear_control_is_silent(tmp_path, name: str) -> None:
    """The linear way to write the same thing reports nothing at all."""
    findings = _scan_source(tmp_path, CONTROLS[name])
    assert findings == [], (
        f"control {name!r} produced {[(f.code, f.message) for f in findings]}")


def test_membership_against_a_set_is_not_a_finding(tmp_path) -> None:
    """The false positive that buried every real finding, pinned.

    `x in s` is O(1) for a set, so it is not a candidate however deep the loop.
    """
    findings = _scan_source(tmp_path, """
        def g(rows: list[str], members: frozenset[str]):
            for r in rows:
                for _ in range(3):
                    if r in members:
                        yield r
    """)
    assert [f for f in findings if f.code == "SL-IN-LOOP"] == []


def test_iterating_dict_values_is_not_a_linear_method(tmp_path) -> None:
    """`for x in d.values()` is the loop, not an extra linear op inside one."""
    findings = _scan_source(tmp_path, """
        def g(registry: dict[str, int], rows: list[str]):
            for r in rows:
                for value in registry.values():
                    yield r, value
    """)
    assert [f for f in findings if f.code == "SL-LINEAR-METHOD"] == []


def test_accumulator_binding_does_not_suppress_its_own_finding(tmp_path) -> None:
    """`s += p` binds `s`; recording that before checking hid every one."""
    findings = _scan_source(tmp_path, """
        def g(parts: list[str]):
            s: str = ""
            for p in parts:
                s += p
            return s
    """)
    accum = [f for f in findings if f.code == "SL-ACCUM-ADD"]
    assert accum and accum[0].confidence == "high"


def test_comprehension_body_is_scanned_as_a_loop(tmp_path) -> None:
    """A comprehension is a loop; its body gets the same rules as a `for`."""
    findings = _scan_source(tmp_path, """
        def g(items: list[str], seq: list[str]):
            return [seq.index(x) for x in items]
    """)
    assert "SL-LINEAR-METHOD" in {f.code for f in findings}


def test_infer_kinds_prefers_the_annotation_over_the_value() -> None:
    """`seen: set[str] = _build()` is a set, whatever `_build` looks like."""
    import ast
    fn = ast.parse(textwrap.dedent("""
        def g(rows: list[str], lookup: Mapping[str, int]):
            seen: set[str] = _build()
            plain = []
            return seen, plain
    """)).body[0]
    kinds = infer_kinds(fn)
    assert kinds["rows"] == "list"
    assert kinds["lookup"] == "dict"
    assert kinds["seen"] == "set"
    assert kinds["plain"] == "list"


# --- C ----------------------------------------------------------------------

C_PLANTED = """
#include <string.h>
#include <stdlib.h>

void f_nest(int n, int *a) {
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            a[i] += j;
        }
    }
}

void f_memmove_loop(char *buf, int n) {
    for (int i = 0; i < n; i++) {
        memmove(buf, buf + 1, n - i);
    }
}

void f_strlen_cond(char *s) {
    for (size_t i = 0; i < strlen(s); i++) {
        s[i] = 'x';
    }
}

char *f_realloc_loop(char *p, int n) {
    for (int i = 0; i < n; i++) {
        p = realloc(p, i + 1);
    }
    return p;
}
"""

C_CONTROL = """
#include <string.h>

void g_flat(int n, int *a) {
    for (int i = 0; i < n; i++) { a[i] = i; }
    for (int j = 0; j < n; j++) { a[j] += 1; }
}

void g_strlen_hoisted(char *s) {
    size_t len = strlen(s);
    for (size_t i = 0; i < len; i++) { s[i] = 'x'; }
}

void g_single_memmove(char *buf, int n) {
    memmove(buf, buf + 1, n);
}
"""

C_WAIVED = """
#include <string.h>

void f_waived(char *buf, int n) {
    for (int i = 0; i < n; i++) {
        /* native-lint: allow NC001 -- bounded by the connection's slab count */
        memmove(buf, buf + 1, n);
    }
}
"""


def _scan_c_source(tmp_path, source: str) -> list[Finding]:
    unit = tmp_path / "subject.c"
    unit.write_text(source, encoding="utf-8")
    return scan_c(unit, tmp_path)


@pytest.mark.parametrize("code", [
    "CL-NEST", "CL-LINEAR-IN-LOOP", "CL-STRLEN-COND", "CL-REALLOC-IN-LOOP",
])
def test_c_planted_defect_is_reported(tmp_path, code: str) -> None:
    findings = _scan_c_source(tmp_path, C_PLANTED)
    assert code in {f.code for f in findings}


def test_c_linear_control_is_silent(tmp_path) -> None:
    assert _scan_c_source(tmp_path, C_CONTROL) == []


def test_c_findings_name_their_function(tmp_path) -> None:
    """Attribution to `<file>` is useless; a trailing comment used to cause it."""
    findings = _scan_c_source(tmp_path, C_PLANTED)
    by_code = {f.code: f.func for f in findings}
    assert by_code["CL-NEST"] == "f_nest"
    assert by_code["CL-LINEAR-IN-LOOP"] == "f_memmove_loop"
    assert by_code["CL-STRLEN-COND"] == "f_strlen_cond"


def test_c_signature_with_a_trailing_comment_still_names_the_function(tmp_path) -> None:
    findings = _scan_c_source(tmp_path, """
void f_commented(int n, int *a) {   /* a trailing comment */
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) { a[i] += j; }
    }
}
""")
    assert [f.func for f in findings] == ["f_commented"]


def test_c_waiver_suppresses_the_following_line(tmp_path) -> None:
    """The same `native-lint: allow` comments this tree already uses."""
    assert _scan_c_source(tmp_path, C_WAIVED) == []


# --- the ratchet ------------------------------------------------------------

def test_discover_walks_python_and_c(tmp_path) -> None:
    (tmp_path / "mod.py").write_text(textwrap.dedent(PLANTED["SL-IN-LOOP"]),
                                     encoding="utf-8")
    (tmp_path / "unit.c").write_text(C_PLANTED, encoding="utf-8")
    codes = {f.code for f in discover(tmp_path)}
    assert "SL-IN-LOOP" in codes
    assert "CL-NEST" in codes


def test_finding_key_survives_a_line_shift(tmp_path) -> None:
    """Adding an import must not re-report every finding in the module as new."""
    before = _scan_source(tmp_path, PLANTED["SL-IN-LOOP"])
    after = _scan_source(tmp_path, "import os\nimport sys\n"
                         + textwrap.dedent(PLANTED["SL-IN-LOOP"]))
    assert {f.key for f in before} == {f.key for f in after}
    assert [f.line for f in before] != [f.line for f in after]


def test_discovery_baseline_covers_every_current_candidate() -> None:
    """`--discover-check` is green on the tree as committed.

    This is the ratchet: a new superlinear shape lands as a failure here, not as
    one more line in a report nobody reads.
    """
    from wreath._devtools.complexity_probe import _discovery_path, _run_discovery

    path = _discovery_path()
    assert path.exists(), "run wreath-complexity-probe --update-discovery"
    known = set(json.loads(path.read_text(encoding="utf-8"))["keys"])
    fresh = sorted({f.key for f in _run_discovery()} - known)
    assert fresh == [], (
        "unacknowledged superlinear candidates: " + ", ".join(fresh))
