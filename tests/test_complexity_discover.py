from __future__ import annotations

import json
import textwrap

import pytest

from wreath._devtools.complexity_discover import (
    Finding,
    _owned_statements,
    _source_fingerprint,
    discover,
    infer_kinds,
    scan_c,
    scan_python,
)

# Each entry: rule code, the planted body that must fire it, and the linear
# control that must not.
PLANTED = {
    "SL-IN-LOOP": """
        def f(items: list[str], haystack: list[str]):
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
        def f(buf: bytes, n):
            for _ in range(n):
                head = buf[:]
            return head
    """,
    "SL-LINEAR-METHOD": """
        def f(items: list[str], seq: list[str]):
            return [seq.index(x) for x in items]
    """,
    "SL-LINEAR-CALL": """
        def f(items: list[str], seq: list[str]):
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
        def f(items: list[str], other: list[str]):
            for _ in items:
                z = [y for y in other]
            return z
    """,
    "SL-COMP-NEST": """
        def f(rows: list[str]):
            return [(left, right) for left in rows for right in rows]
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
    "unknown-membership-cost": """
        def g(rows, members):
            for row in rows:
                if row in members:
                    yield row
    """,
    "loop-local-linear-argument": """
        def g(rows: list[list[str]]):
            out: list[str] = []
            for row in rows:
                out.extend(row)
            return out
    """,
    "for-iterator-evaluated-once": """
        def g(rows: list[str]):
            for row in sorted(rows):
                yield row
    """,
    "for-slice-evaluated-once": """
        def g(rows: list[str]):
            for row in rows[1:]:
                yield row
    """,
    "return-executes-once": """
        def g(rows: list[str], found: list[str]):
            for row in rows:
                if row:
                    return sorted(found)
            return []
    """,
    "fixed-width-slice": """
        def g(data: bytes):
            for index in range(len(data)):
                yield data[index:index + 4]
    """,
    "bounded-prefix-slice": """
        def g(rows: list[str], version: tuple[int, ...]):
            for row in rows:
                yield row, version[:1]
    """,
    "reversed-view": """
        def g(groups: list[list[str]]):
            for group in groups:
                for value in reversed(group):
                    yield value
    """,
}


def _scan_source(tmp_path, source: str) -> list[Finding]:
    module = tmp_path / "subject.py"
    module.write_text(textwrap.dedent(source), encoding="utf-8")
    return scan_python(module, tmp_path)


@pytest.mark.parametrize("code", sorted(PLANTED))
def test_planted_defect_is_reported(tmp_path, code: str) -> None:
    findings = _scan_source(tmp_path, PLANTED[code])
    assert code in {f.code for f in findings}, (
        f"{code} did not fire on its own planted defect; got {sorted({f.code for f in findings})}"
    )


@pytest.mark.parametrize("name", sorted(CONTROLS))
def test_linear_control_is_silent(tmp_path, name: str) -> None:
    findings = _scan_source(tmp_path, CONTROLS[name])
    assert findings == [], f"control {name!r} produced {[(f.code, f.message) for f in findings]}"


def test_membership_against_a_set_is_not_a_finding(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def g(rows: list[str], members: frozenset[str]):
            for r in rows:
                for _ in range(3):
                    if r in members:
                        yield r
    """,
    )
    assert [f for f in findings if f.code == "SL-IN-LOOP"] == []


def test_iterating_dict_values_is_not_a_linear_method(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def g(registry: dict[str, int], rows: list[str]):
            for r in rows:
                for value in registry.values():
                    yield r, value
    """,
    )
    assert [f for f in findings if f.code == "SL-LINEAR-METHOD"] == []


def test_accumulator_binding_does_not_suppress_its_own_finding(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def g(parts: list[str]):
            s: str = ""
            for p in parts:
                s += p
            return s
    """,
    )
    accum = [f for f in findings if f.code == "SL-ACCUM-ADD"]
    assert accum and accum[0].confidence == "high"


def test_cached_recursion_is_not_a_finding(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        @cache
        def fib(n):
            if n < 2:
                return n
            return fib(n - 1) + fib(n - 2)
    """,
    )
    assert [finding for finding in findings if finding.code == "SL-RECURSE"] == []


def test_calls_hidden_behind_a_nested_scope_are_not_branching_recursion(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def outer():
            def inner(n):
                if n:
                    return outer()
                return outer()
            return inner
    """,
    )
    assert [finding for finding in findings if finding.code == "SL-RECURSE"] == []


def test_python_waiver_needs_a_named_rule_and_reason(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def g(rows: list[str], members: list[str]):
            for row in rows:
                # complexity: allow SL-IN-LOOP -- rows and members are capped at 8
                if row in members:
                    yield row
    """,
    )
    assert findings == []


def test_comprehension_body_is_scanned_as_a_loop(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def g(items: list[str], seq: list[str]):
            return [seq.index(x) for x in items]
    """,
    )
    assert "SL-LINEAR-METHOD" in {f.code for f in findings}


def test_infer_kinds_prefers_the_annotation_over_the_value() -> None:
    import ast

    fn = ast.parse(
        textwrap.dedent("""
        def g(rows: list[str], lookup: Mapping[str, int]):
            seen: set[str] = _build()
            plain = []
            return seen, plain
    """)
    ).body[0]
    kinds = infer_kinds(fn)
    assert kinds["rows"] == "list"
    assert kinds["lookup"] == "dict"
    assert kinds["seen"] == "set"
    assert kinds["plain"] == "list"


def test_kind_inference_visits_each_scope_once_with_a_same_size_control() -> None:
    import ast

    count = 40
    siblings = "\n".join(
        f"def function_{index}(items):\n    local = list(items)\n    return local"
        for index in range(count)
    )
    nested_lines = ["    " * index + f"def function_{index}(items):" for index in range(count)]
    nested_lines.append("    " * count + "local = list(items)")
    nested_lines.extend(
        "    " * (index + 1) + f"return function_{index + 1}"
        for index in reversed(range(count - 1))
    )

    def visits(source: str) -> int:
        tree = ast.parse(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        return sum(1 for function in functions for _ in _owned_statements(function))

    sibling_visits = visits(siblings)
    chain_visits = visits("\n".join(nested_lines))

    assert chain_visits <= sibling_visits * 2


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

void f_strlen_cond(char *s) {
    for (size_t i = 0; i < strlen(s); i++) {
        s[i] = 'x';
    }
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
    /* complexity: allow CL-STRLEN-COND -- input is capped at 16 bytes */
    for (size_t i = 0; i < strlen(buf); i++) { buf[i] = 'x'; }
}
"""


def _scan_c_source(tmp_path, source: str) -> list[Finding]:
    unit = tmp_path / "subject.c"
    unit.write_text(source, encoding="utf-8")
    return scan_c(unit, tmp_path)


@pytest.mark.parametrize(
    "code",
    [
        "CL-NEST-SAME",
        "CL-STRLEN-COND",
    ],
)
def test_c_planted_defect_is_reported(tmp_path, code: str) -> None:
    findings = _scan_c_source(tmp_path, C_PLANTED)
    assert code in {f.code for f in findings}


def test_c_linear_control_is_silent(tmp_path) -> None:
    assert _scan_c_source(tmp_path, C_CONTROL) == []


def test_c_findings_name_their_function(tmp_path) -> None:
    findings = _scan_c_source(tmp_path, C_PLANTED)
    by_code = {f.code: f.func for f in findings}
    assert by_code["CL-NEST-SAME"] == "f_nest"
    assert by_code["CL-STRLEN-COND"] == "f_strlen_cond"


def test_c_signature_with_a_trailing_comment_still_names_the_function(tmp_path) -> None:
    findings = _scan_c_source(
        tmp_path,
        """
void f_commented(int n, int *a) {   /* a trailing comment */
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) { a[i] += j; }
    }
}
""",
    )
    assert [f.func for f in findings] == ["f_commented"]


def test_c_signature_with_return_type_on_prior_line_names_function(tmp_path) -> None:
    findings = _scan_c_source(
        tmp_path,
        """
static int
f_split(int n, int *a)
{
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) { a[i] += j; }
    }
    return 0;
}
""",
    )
    assert [f.func for f in findings] == ["f_split"]


def test_c_waiver_suppresses_the_following_line(tmp_path) -> None:
    assert _scan_c_source(tmp_path, C_WAIVED) == []


def test_discover_walks_python_and_c(tmp_path) -> None:
    (tmp_path / "mod.py").write_text(textwrap.dedent(PLANTED["SL-IN-LOOP"]), encoding="utf-8")
    (tmp_path / "unit.c").write_text(C_PLANTED, encoding="utf-8")
    codes = {f.code for f in discover(tmp_path)}
    assert "SL-IN-LOOP" in codes
    assert "CL-NEST-SAME" in codes


def test_discover_reuses_only_byte_identical_cached_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text(textwrap.dedent(PLANTED["SL-IN-LOOP"]), encoding="utf-8")
    second.write_text("def clean():\n    return 1\n", encoding="utf-8")
    original = discover(tmp_path)
    grouped = {
        path.name: tuple(finding for finding in original if finding.file == path.name)
        for path in (first, second)
    }
    cache = {path.name: (_source_fingerprint(path), grouped[path.name]) for path in (first, second)}
    scanned: list[str] = []
    original_scan = scan_python

    def recording_scan(path, root):
        scanned.append(path.name)
        return original_scan(path, root)

    monkeypatch.setattr(
        "wreath._devtools.complexity_discover.scan_python",
        recording_scan,
    )

    assert discover(tmp_path, cache=cache) == original
    assert scanned == []

    second.write_text(textwrap.dedent(PLANTED["SL-ACCUM-ADD"]), encoding="utf-8")
    changed = discover(tmp_path, cache=cache)
    assert scanned == ["second.py"]
    assert any(finding.file == "second.py" for finding in changed)


def test_repository_discovery_cache_is_invalidated_with_its_scanner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath._devtools import complexity_probe

    source_root = tmp_path / "src" / "wreath"
    source_root.mkdir(parents=True)
    module = source_root / "clean.py"
    module.write_text("def clean():\n    return 1\n", encoding="utf-8")
    cached = Finding(
        file="clean.py",
        line=1,
        code="SL-RECURSE",
        func="clean",
        depth=0,
        confidence="low",
        message="cached sentinel",
        source="",
    )
    baseline = tmp_path / complexity_probe.DISCOVERY_PATH
    baseline.parent.mkdir(parents=True)
    document = {
        "version": complexity_probe.DISCOVERY_VERSION,
        "scanner": complexity_probe._discovery_scanner_identity(),
        "sources": {"clean.py": _source_fingerprint(module)},
        "keys": [cached.key],
        "candidates": [cached.document()],
    }
    baseline.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(complexity_probe, "repo_root", lambda: tmp_path)

    assert complexity_probe._run_discovery() == [cached]

    document["scanner"] = {"source": "changed", "python": "changed"}
    baseline.write_text(json.dumps(document), encoding="utf-8")
    assert complexity_probe._run_discovery() == []


def test_finding_key_survives_a_line_shift(tmp_path) -> None:
    before = _scan_source(tmp_path, PLANTED["SL-IN-LOOP"])
    after = _scan_source(
        tmp_path, "import os\nimport sys\n" + textwrap.dedent(PLANTED["SL-IN-LOOP"])
    )
    assert {f.key for f in before} == {f.key for f in after}
    assert [f.line for f in before] != [f.line for f in after]


def test_finding_key_distinguishes_sites_with_the_same_rule(tmp_path) -> None:
    findings = _scan_source(
        tmp_path,
        """
        def f(left: list[int], right: list[int], rows: list[int]) -> None:
            for row in rows:
                if row in left:
                    pass
                if row in right:
                    pass
        """,
    )
    assert len(findings) == 2
    assert len({finding.key for finding in findings}) == 2


def test_discovery_baseline_covers_every_current_candidate() -> None:
    from wreath._devtools.complexity_probe import _discovery_path, _run_discovery

    path = _discovery_path()
    assert path.exists(), "run wreath-complexity-probe --update-discovery"
    known = set(json.loads(path.read_text(encoding="utf-8"))["keys"])
    fresh = sorted({f.key for f in _run_discovery()} - known)
    assert fresh == [], "unacknowledged superlinear candidates: " + ", ".join(fresh)
