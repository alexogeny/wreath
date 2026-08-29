from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
SOURCE = REPO / "src/wreath/_native/aesgcm.c"
HEADER = REPO / "src/wreath/_native/simd.h"

TEXT = SOURCE.read_text(encoding="utf-8")

#: Anything that needs an instruction set beyond the x86-64 baseline.
INTRINSIC = re.compile(r"\b(?:_mm_[a-z0-9_]+|__m128i)\b")

#: The file puts the return type on the line above the name, so a definition is
#: a name at column zero and its attributes are on the line before.
DEFINITION = re.compile(r"^(?P<signature>[^\n]*)\n(?P<name>wreath_\w+)\s*\(", re.M)


def _guard_state() -> list[tuple[int, frozenset[str]]]:
    """Each line paired with the set of preprocessor conditions holding on it.

    A condition is recorded as its text, with `!` prepended once the `#else`
    arm is entered, so guarded and unguarded code are distinguishable.
    """
    states: list[tuple[int, frozenset[str]]] = []
    stack: list[str] = []
    for number, line in enumerate(TEXT.splitlines(), start=1):
        stripped = line.strip()
        if re.match(r"#\s*if(n?def)?\b", stripped):
            stack.append(stripped)
        elif re.match(r"#\s*el(se|if)\b", stripped) and stack:
            stack[-1] = "!" + stack[-1].lstrip("!")
        elif re.match(r"#\s*endif\b", stripped) and stack:
            stack.pop()
        states.append((number, frozenset(stack)))
    return states


GUARDS = _guard_state()


def _inside_aes_guard(line_number: int) -> bool:
    conditions = GUARDS[line_number - 1][1]
    return any(
        "WREATH_HAVE_AESGCM" in condition and not condition.startswith("!")
        for condition in conditions
    )


def _functions() -> list[tuple[str, str, str]]:
    """(name, signature line, body) for every function defined in the file."""
    matches = list(DEFINITION.finditer(TEXT))
    out: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(TEXT)
        out.append((match.group("name"), match.group("signature"), TEXT[match.start() : end]))
    return out


FUNCTIONS = _functions()


def test_every_intrinsic_is_inside_the_feature_guard() -> None:
    stray = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(TEXT.splitlines(), start=1)
        if INTRINSIC.search(line) and not _inside_aes_guard(number)
    ]
    assert not stray, (
        "aesgcm.c uses an x86 vector type or intrinsic outside "
        "#if defined(WREATH_HAVE_AESGCM); that line is a compile error on every "
        "other architecture:\n  " + "\n  ".join(stray)
    )


def test_every_function_using_an_intrinsic_declares_its_target() -> None:
    missing = [
        name
        for name, signature, body in FUNCTIONS
        if INTRINSIC.search(body) and "WREATH_TARGET_AESGCM" not in signature
    ]
    assert not missing, (
        "these functions use AES-NI/PCLMULQDQ intrinsics without "
        f"WREATH_TARGET_AESGCM on their signature: {missing}"
    )


def test_the_entry_points_exist_outside_the_feature_guard() -> None:
    exported = ("wreath_aesgcm_arms", "wreath_aes128gcm_encrypt", "wreath_aes128gcm_decrypt")
    for name in exported:
        lines = [
            number
            for number, _ in GUARDS
            if re.match(rf"^{name}\s*\(", TEXT.splitlines()[number - 1])
        ]
        assert len(lines) == 1, f"{name} is defined {len(lines)} times, expected once"
        assert not _inside_aes_guard(lines[0]), (
            f"{name} is inside #if defined(WREATH_HAVE_AESGCM); the extension would "
            "have an undefined symbol on every other architecture"
        )

    dispatch = [
        number
        for number, _ in GUARDS
        if re.match(r"^wreath_aesgcm_dispatch\s*\(", TEXT.splitlines()[number - 1])
    ]
    assert len(dispatch) == 1
    assert _inside_aes_guard(dispatch[0])
    assert "wreath_aesgcm_dispatch_scalar" in TEXT


def test_no_function_calls_a_helper_defined_below_it() -> None:
    definitions = {match.group("name"): match.start() for match in DEFINITION.finditer(TEXT)}
    for match in re.finditer(
        r"^static\s+[^;{]*?\b(wreath_\w+)\s*\([^;{]*\)\s*;", TEXT, re.M | re.S
    ):
        definitions[match.group(1)] = min(
            definitions.get(match.group(1), match.start()), match.start()
        )

    offenders: list[str] = []
    for name, _, body in FUNCTIONS:
        body_at = TEXT.index(body)
        for call in re.finditer(r"\b(wreath_\w+)\s*\(", body):
            callee = call.group(1)
            if callee == name or callee not in definitions:
                continue
            if definitions[callee] > body_at + call.start():
                offenders.append(f"{name} calls {callee}, which is defined below it")
    assert not offenders, "\n  ".join(["aesgcm.c has a use before declaration:", *offenders])


def test_the_feature_probe_answers_on_every_architecture() -> None:
    header = HEADER.read_text(encoding="utf-8")
    probe = re.search(r"wreath_simd_has_aesgcm\(void\)\n\{(?P<body>.*?)\n\}", header, re.S)
    assert probe is not None, "simd.h no longer defines wreath_simd_has_aesgcm"
    body = probe.group("body")
    assert "#else" in body and "return 0;" in body, (
        "wreath_simd_has_aesgcm must have a non-x86 arm returning 0; without one "
        "the probe itself is a compile error on aarch64"
    )
    for feature in ("aes", "pclmul", "ssse3"):
        assert f'__builtin_cpu_supports("{feature}")' in body, (
            f"the probe does not test for {feature}, which the kernels use"
        )


def test_the_source_is_registered_in_both_build_files() -> None:
    for path in ("setup.py", "tools/sanitizers/setup_core.py"):
        text = (REPO / path).read_text(encoding="utf-8")
        assert "aesgcm.c" in text, f"{path} does not build src/wreath/_native/aesgcm.c"
