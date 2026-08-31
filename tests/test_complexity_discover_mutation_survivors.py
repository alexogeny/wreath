from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from wreath._devtools.complexity_discover import (
    _c_for_bound,
    _PythonScanner,
    infer_kinds,
    scan_c,
    scan_python,
)


def _python_findings(tmp_path: Path, source: str):
    path = tmp_path / "subject.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return scan_python(path, tmp_path)


def _c_findings(tmp_path: Path, source: str):
    path = tmp_path / "subject.c"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return scan_c(path, tmp_path)


def test_kind_inference_distinguishes_missing_values_and_rebindings() -> None:
    function = ast.parse(
        textwrap.dedent(
            """
            def subject():
                missing: list[str]
                inferred: list[str] = []
                value_inferred: Unknown = []
                declared: set[str] = []
                stable = []
                stable = [1]
                changed = []
                changed = {}
            """
        )
    ).body[0]

    assert isinstance(function, ast.FunctionDef)
    assert infer_kinds(function) == {
        "missing": "list",
        "inferred": "list",
        "value_inferred": "list",
        "declared": "set",
        "stable": "list",
        "changed": None,
    }


def test_unknown_annotation_falls_back_to_the_assigned_value(tmp_path: Path) -> None:
    findings = _python_findings(
        tmp_path,
        """
        def subject(rows: list[int]):
            members: Unknown = []
            for row in rows:
                if row in members:
                    pass
        """,
    )

    assert {finding.code for finding in findings} == {"SL-IN-LOOP"}


def test_report_ignores_waivers_outside_the_nodes_source_line(tmp_path: Path) -> None:
    source = "value = 1\n# complexity: allow TEST -- unrelated\n"
    scanner = _PythonScanner(tmp_path / "subject.py", source, tmp_path)
    node = ast.Constant(value=None)
    node.lineno = 0

    scanner._report(node, "TEST", "message")

    assert len(scanner.findings) == 1
    assert scanner.findings[0].func == "<module>"
    assert scanner.findings[0].source == ""


def test_python_findings_preserve_scope_source_and_recursion_depth(tmp_path: Path) -> None:
    findings = _python_findings(
        tmp_path,
        """
        def fib(n):
            if n < 2:
                return n
            return fib(n - 1) + fib(n - 2)

        def subject(rows: list[int], members: list[int]):
            for row in rows:
                if row in members:
                    pass
        """,
    )

    by_code = {finding.code: finding for finding in findings}
    assert by_code["SL-IN-LOOP"].func == "subject"
    assert by_code["SL-IN-LOOP"].source == "if row in members:"
    assert by_code["SL-RECURSE"].func == "fib"
    assert by_code["SL-RECURSE"].depth == 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("values.pop(0)", {"SL-FRONT-MUTATE"}),
        ("values.pop(1)", set()),
        ("build().pop(0)", set()),
        ("values.copy()", {"SL-LINEAR-METHOD"}),
        ("values.index(item, 1)", set()),
    ],
)
def test_receiver_linear_methods_require_their_exact_shape(
    tmp_path: Path, body: str, expected: set[str]
) -> None:
    findings = _python_findings(
        tmp_path,
        f"""
        def subject(rows: list[int], values: list[int]):
            for item in rows:
                {body}
        """,
    )

    assert {finding.code for finding in findings} == expected


@pytest.mark.parametrize(
    ("receiver_annotation", "expression", "expected"),
    [
        ("str", "receiver.join(values)", True),
        ("bytes", "receiver.join(values)", True),
        ("list[str]", "receiver.join(values)", False),
        ("str", "receiver.extend(values)", False),
        ("list[str]", "receiver.extend(values)", True),
        ("dict[str, str]", "receiver.extend(values)", False),
        ("dict[str, str]", "receiver.update(values)", True),
        ("list[str]", "receiver.update(values)", False),
        ("str", "receiver.extend()", False),
        ("list[str]", "receiver.extend()", False),
    ],
)
def test_argument_linear_methods_require_supported_receivers_and_arguments(
    tmp_path: Path,
    receiver_annotation: str,
    expression: str,
    expected: bool,
) -> None:
    findings = _python_findings(
        tmp_path,
        f"""
        def subject(
            rows: list[str],
            receiver: {receiver_annotation},
            values: list[str],
        ):
            for row in rows:
                {expression}
        """,
    )

    assert ("SL-LINEAR-METHOD" in {finding.code for finding in findings}) is expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('"".join(values)', True),
        ("b''.join(values)", True),
        ("(1).join(values)", False),
        ("patterns.compile(row)", False),
        ("re.match(row)", False),
        ("re.compile(row)", True),
    ],
)
def test_literal_join_and_regex_compile_require_their_named_receiver(
    tmp_path: Path, expression: str, expected: bool
) -> None:
    findings = _python_findings(
        tmp_path,
        f"""
        def subject(rows: list[str], values: list[str], patterns):
            for row in rows:
                {expression}
        """,
    )

    assert bool(findings) is expected


@pytest.mark.parametrize(
    ("slice_expression", "expected"),
    [
        ("data[1:4]", False),
        ("data[1:]", True),
        ("data[index:]", False),
        ("data[:limit]", True),
        ("data[:index]", False),
        ("data[:4]", False),
        ('data[:"end"]', True),
    ],
)
def test_loop_slices_distinguish_open_fixed_and_varying_bounds(
    tmp_path: Path, slice_expression: str, expected: bool
) -> None:
    findings = _python_findings(
        tmp_path,
        f"""
        def subject(data: bytes, limit: int):
            for index in range(len(data)):
                yield {slice_expression}
        """,
    )

    assert ("SL-SLICE-LOOP" in {finding.code for finding in findings}) is expected


def test_nested_comprehension_requires_a_proven_container_kind(tmp_path: Path) -> None:
    typed = _python_findings(
        tmp_path,
        """
        def subject(rows: list[int]):
            return [(left, right) for left in rows for right in rows]
        """,
    )
    unknown = _python_findings(
        tmp_path,
        """
        def subject(rows):
            return [(left, right) for left in rows for right in rows]
        """,
    )

    assert "SL-COMP-NEST" in {finding.code for finding in typed}
    assert "SL-COMP-NEST" not in {finding.code for finding in unknown}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("for (int i = 0; i < n)", None),
        ("for (int i = 0; i < n; i++)", "n"),
        ("for (int i = 0; n > i; i++)", "n"),
    ],
)
def test_c_for_bound_requires_three_clauses_and_accepts_reverse_conditions(
    line: str, expected: str | None
) -> None:
    assert _c_for_bound(line) == expected


def test_c_scan_supports_paths_outside_the_display_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = tmp_path / "subject.c"
    path.write_text("int value;\n", encoding="utf-8")

    assert scan_c(path, root) == []


def test_c_waiver_survives_blank_lines_before_the_code(tmp_path: Path) -> None:
    findings = _c_findings(
        tmp_path,
        """
        #include <string.h>
        void subject(char *value) {
            /* complexity: allow CL-STRLEN-COND -- value is capped */

            for (int i = 0; i < strlen(value); i++) { value[i] = 'x'; }
        }
        """,
    )

    assert findings == []


def test_c_nested_block_does_not_replace_the_enclosing_function(tmp_path: Path) -> None:
    findings = _c_findings(
        tmp_path,
        """
        void subject(int n) {
            if (n) {
                for (int i = 0; i < n; i++) {
                    for (int j = 0; j < n; j++) { n += j; }
                }
            }
        }
        """,
    )

    assert [finding.func for finding in findings] == ["subject"]


def test_c_nested_declaration_shape_does_not_replace_the_enclosing_function(
    tmp_path: Path,
) -> None:
    findings = _c_findings(
        tmp_path,
        """void subject(int n) {
inner(int value) {
for (int i = 0; i < n; i++) {
for (int j = 0; j < n; j++) { n += j; }
}
}
}
""",
    )

    assert [finding.func for finding in findings] == ["subject"]


def test_c_function_scope_ends_before_file_scope_findings(tmp_path: Path) -> None:
    findings = _c_findings(
        tmp_path,
        """
        void subject(void) {
        }
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < n; j++) { n += j; }
        }
        """,
    )

    assert [finding.func for finding in findings] == ["<file>"]


def test_c_nested_closing_brace_does_not_end_function_scope(tmp_path: Path) -> None:
    findings = _c_findings(
        tmp_path,
        """
        void subject(int n) {
            if (n) {
                n += 1;
            }
            for (int i = 0; i < n; i++) {
                for (int j = 0; j < n; j++) { n += j; }
            }
        }
        """,
    )

    assert [finding.func for finding in findings] == ["subject"]


def test_c_nested_unbounded_loops_are_not_same_bound_findings(tmp_path: Path) -> None:
    findings = _c_findings(
        tmp_path,
        """
        void subject(int ready) {
            while (ready) {
                while (ready) { ready = 0; }
            }
        }
        """,
    )

    assert findings == []
