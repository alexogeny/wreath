from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .engine import _atomic_write, _fsync_directory, _validate_artifacts

_SANITIZERS = ("address", "undefined")
_COVERAGE = "trace-cmp,trace-div,indirect-calls"
_WALL_CLOCK_GRACE_SECONDS = 2
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class NativeFuzzTarget:
    name: str
    harness: Path
    python_target: str
    build_scripts: tuple[Path, ...] = (Path("tools/sanitizers/setup_core.py"),)
    extension_globs: tuple[str, ...] = ("_core*.so",)


@dataclass(frozen=True, slots=True)
class NativeCampaignConfig:
    project_root: Path
    build_root: Path
    corpus_root: Path
    artifact_root: Path
    max_seconds: int = 60
    max_runs: int = 0
    max_input_size: int = 65_536
    max_input_seconds: int = 2
    seed: int = 1
    rebuild: bool = True
    replay_only: bool = False
    max_minimize_seconds: int = 10
    max_build_seconds: int = 300

    def __post_init__(self) -> None:
        for name in ("project_root", "build_root", "corpus_root", "artifact_root"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        for name in (
            "max_seconds",
            "max_input_size",
            "max_input_seconds",
            "max_minimize_seconds",
            "max_build_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_runs, bool)
            or not isinstance(self.max_runs, int)
            or self.max_runs < 0
        ):
            raise ValueError("max_runs must be a non-negative integer")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**32
        ):
            raise ValueError("seed must be an integer from 0 through 2**32 - 1")
        if not isinstance(self.rebuild, bool):
            raise ValueError("rebuild must be a boolean")
        if not isinstance(self.replay_only, bool):
            raise ValueError("replay_only must be a boolean")
        if self.replay_only and self.max_runs:
            raise ValueError("replay_only requires max_runs=0; omit the run-count cap")


@dataclass(frozen=True, slots=True)
class NativeCampaignReport:
    target: str
    seed: int
    exit_code: int
    command: tuple[str, ...]
    findings: tuple[Path, ...]
    cases_executed: int
    coverage_features: int
    fuzzer_features: int
    peak_rss_mb: int
    corpus_size: int
    corpus_added: int
    corpus_addition_digests: tuple[str, ...]
    corpus_digests: tuple[str, ...]
    stdout: str
    stderr: str
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class NativeMergeReport:
    target: str
    before: int
    after: int
    command: tuple[str, ...]
    stdout: str
    stderr: str


NATIVE_TARGETS = (
    NativeFuzzTarget(
        "graphql-parser",
        Path("tools/fuzz_native/graphql_harness.c"),
        "graphql-parser",
    ),
    NativeFuzzTarget(
        "h2-frames",
        Path("tools/fuzz_native/h2_harness.c"),
        "h2-frames",
        build_scripts=(
            Path("tools/sanitizers/setup_core.py"),
            Path("tools/sanitizers/setup_server.py"),
        ),
        extension_globs=("_core*.so", "_server*.so"),
    ),
    NativeFuzzTarget(
        "http-replay-codec",
        Path("tools/fuzz_native/http_replay_harness.c"),
        "http-replay-codec",
    ),
    NativeFuzzTarget("http1-parser", Path("tools/fuzz_native/http1_harness.c"), "http1-parser"),
    NativeFuzzTarget(
        "multipart-parser",
        Path("tools/fuzz_native/multipart_harness.c"),
        "multipart-parser",
    ),
    NativeFuzzTarget("xml-parser", Path("tools/fuzz_native/xml_harness.c"), "xml-parser"),
)
_BY_NAME = {target.name: target for target in NATIVE_TARGETS}


def native_target(name: str) -> NativeFuzzTarget:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"unknown native fuzz target {name!r}; choose one of: {', '.join(_BY_NAME)}"
        ) from None


def _compiler() -> str | None:
    return shutil.which("clang")


def _run_checked(command: list[str], *, timeout: int, **kwargs) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as error:
        diagnostic = (_timeout_text(error.stdout) + _timeout_text(error.stderr))[-8_000:]
        raise RuntimeError(
            f"native fuzz build command {shlex.join(command)} exceeded its "
            f"{timeout}-second controller timeout:\n{diagnostic}"
        ) from error
    if result.returncode:
        diagnostic = (result.stdout + result.stderr)[-8_000:]
        raise RuntimeError(f"native fuzz build failed ({result.returncode}):\n{diagnostic}")
    return result


def _python_build_identity() -> dict[str, object]:
    return {
        "executable": sys.executable,
        "version": list(sys.version_info[:3]),
        "cache_tag": sys.implementation.cache_tag,
        "soabi": sysconfig.get_config_var("SOABI"),
        "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
        "ldlibrary": sysconfig.get_config_var("LDLIBRARY"),
        "platform": sysconfig.get_platform(),
    }


def _compiler_build_identity(compiler: str, timeout: int) -> dict[str, str]:
    result = _run_checked([compiler, "--version"], timeout=timeout)
    version = (result.stdout + result.stderr).strip()
    if not version:
        raise RuntimeError(
            f"native fuzz compiler {compiler} returned no version identity; "
            "use a compiler that reports its version"
        )
    return {"path": compiler, "version": version}


def _identity_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _library_name() -> str:
    library = str(sysconfig.get_config_var("LDLIBRARY") or "")
    if not library.startswith("lib"):
        raise RuntimeError("Python build does not expose an embeddable LDLIBRARY")
    return library.removeprefix("lib").split(".so", 1)[0].split(".dylib", 1)[0]


def _instrumented_binary(path: Path) -> bool:
    data = path.read_bytes()
    return all(marker in data for marker in (b"__asan_", b"__ubsan_", b"__sanitizer_cov"))


def _target_library(target: NativeFuzzTarget, config: NativeCampaignConfig) -> Path:
    return config.build_root / "targets" / target.name / "lib"


def _source_digest(target: NativeFuzzTarget, project_root: Path) -> str:
    source_root = project_root / "src/wreath"
    paths = {
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix not in {".pyc", ".so"} and "__pycache__" not in path.parts
    }
    paths.update(
        project_root / path
        for path in (
            target.harness,
            Path("tools/fuzz_native/harness.c"),
            Path("tools/fuzz_native/harness.h"),
            *target.build_scripts,
        )
    )
    digest = hashlib.sha256()
    for path in sorted(paths):
        try:
            relative = path.relative_to(project_root)
            data = path.read_bytes()
        except OSError as error:
            raise ValueError(
                f"native fuzz build input {path} is missing or unreadable; rebuild from "
                "a complete source checkout"
            ) from error
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _build_manifest(
    executable: Path,
    extensions: tuple[Path, ...],
    target: NativeFuzzTarget,
    compiler: dict[str, str],
    source_digest: str,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema": 4,
        "target": target.name,
        "compiler": compiler,
        "python": _python_build_identity(),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "extensions": [
            {
                "path": str(extension),
                "sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
            }
            for extension in extensions
        ],
        "extension_globs": list(target.extension_globs),
        "sanitizers": list(_SANITIZERS),
        "sanitizer_coverage": _COVERAGE,
        "source_sha256": source_digest,
    }
    manifest["build_identity"] = _identity_digest(manifest)
    return manifest


def build_native_target(target: NativeFuzzTarget, config: NativeCampaignConfig) -> Path:
    compiler = _compiler()
    if compiler is None:
        raise RuntimeError("native fuzzing requires clang with libFuzzer support")
    source_package = config.project_root / "src/wreath"
    harness = config.project_root / target.harness
    requirements = [
        (source_package, "a src/wreath package directory"),
        (harness, str(target.harness)),
    ]
    requirements.extend(
        (config.project_root / script, str(script)) for script in target.build_scripts
    )
    for path, form in requirements:
        if not path.exists():
            raise ValueError(f"native fuzz build needs {path}; provide {form}")
    source_digest = _source_digest(target, config.project_root)
    compiler_identity = _compiler_build_identity(compiler, config.max_build_seconds)

    target_build = config.build_root / "targets" / target.name
    library = _target_library(target, config)
    temporary = target_build / "temp"
    shutil.copytree(
        source_package,
        library / "wreath",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.so", "__pycache__"),
    )
    instrumentation = (
        "-O1 -g -fno-omit-frame-pointer "
        "-fsanitize=fuzzer-no-link,address,undefined "
        f"-fsanitize-coverage={_COVERAGE}"
    )
    environment = dict(os.environ)
    environment.update(
        {"CC": compiler, "LDSHARED": f"{compiler} -shared", "CFLAGS": instrumentation}
    )
    for script in target.build_scripts:
        _run_checked(
            [
                sys.executable,
                str(config.project_root / script),
                "build_ext",
                "--build-lib",
                str(library),
                "--build-temp",
                str(temporary),
                "--force",
            ],
            cwd=config.project_root,
            env=environment,
            timeout=config.max_build_seconds,
        )
    extensions = []
    for pattern in target.extension_globs:
        matches = tuple((library / "wreath/_native").glob(pattern))
        if len(matches) != 1:
            raise RuntimeError(
                f"native fuzz build expected one instrumented {pattern} extension, found {matches}"
            )
        extensions.append(matches[0])
    built_extensions = tuple(extensions)

    executable = config.build_root / "bin" / target.name
    executable.parent.mkdir(parents=True, exist_ok=True)
    include = str(sysconfig.get_config_var("INCLUDEPY"))
    libdir = str(sysconfig.get_config_var("LIBDIR"))
    system_libraries = shlex.split(
        f"{sysconfig.get_config_var('LIBS') or ''} {sysconfig.get_config_var('SYSLIBS') or ''}"
    )
    command = [
        compiler,
        "-std=c11",
        "-O1",
        "-g",
        "-fno-omit-frame-pointer",
        "-fsanitize=fuzzer,address,undefined",
        f"-fsanitize-coverage={_COVERAGE}",
        f"-I{include}",
        f"-I{config.project_root / 'tools/fuzz_native'}",
        str(config.project_root / "tools/fuzz_native/harness.c"),
        str(harness),
        f"-L{libdir}",
        f"-Wl,-rpath,{libdir}",
        f"-l{_library_name()}",
        *system_libraries,
        "-o",
        str(executable),
    ]
    _run_checked(command, cwd=config.project_root, timeout=config.max_build_seconds)
    if not _instrumented_binary(executable) or any(
        not _instrumented_binary(extension) for extension in built_extensions
    ):
        raise RuntimeError(
            "native fuzz build completed without ASan, UBSan, and SanitizerCoverage symbols"
        )
    manifest = _build_manifest(
        executable, built_extensions, target, compiler_identity, source_digest
    )
    _atomic_write(
        executable.with_suffix(".build.json"),
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    return executable


def _validate_build(
    executable: Path, target: NativeFuzzTarget, config: NativeCampaignConfig
) -> str:
    manifest_path = executable.with_suffix(".build.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(
            f"native fuzz executable {executable} has no valid build manifest; rebuild it"
        ) from error
    if not isinstance(manifest, dict):
        raise ValueError(
            f"native fuzz executable {executable} has no valid build manifest; rebuild it"
        )
    expected_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    compiler = _compiler()
    compiler_identity = (
        _compiler_build_identity(compiler, config.max_build_seconds)
        if compiler is not None
        else None
    )
    extension_values = manifest.get("extensions")
    extensions: list[tuple[Path, str]] = []
    if isinstance(extension_values, list):
        for value in extension_values:
            if not isinstance(value, dict):
                break
            path = value.get("path")
            digest = value.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                break
            extensions.append((Path(path), digest))
    recorded_build_identity = manifest.get("build_identity")
    manifest_without_identity = {
        key: value for key, value in manifest.items() if key != "build_identity"
    }
    if (
        manifest.get("schema") != 4
        or manifest.get("target") != target.name
        or manifest.get("python") != _python_build_identity()
        or manifest.get("compiler") != compiler_identity
        or manifest.get("executable_sha256") != expected_digest
        or manifest.get("sanitizers") != list(_SANITIZERS)
        or manifest.get("sanitizer_coverage") != _COVERAGE
        or manifest.get("source_sha256") != _source_digest(target, config.project_root)
        or manifest.get("extension_globs") != list(target.extension_globs)
        or len(extensions) != len(target.extension_globs)
        or not _instrumented_binary(executable)
        or any(
            not extension.is_file()
            or digest != hashlib.sha256(extension.read_bytes()).hexdigest()
            or not _instrumented_binary(extension)
            for extension, digest in extensions
        )
        or not isinstance(recorded_build_identity, str)
        or recorded_build_identity != _identity_digest(manifest_without_identity)
    ):
        raise ValueError(
            f"native fuzz executable {executable} does not match the recorded binaries "
            "and current native fuzz sources, current Python ABI, platform, and compiler; "
            "rebuild it"
        )
    return recorded_build_identity


def _validate_native_corpus(corpus: Path, max_input_size: int) -> tuple[str, ...]:
    corpus.mkdir(parents=True, exist_ok=True)
    digests = []
    for path in sorted(corpus.iterdir()):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix != ".input"
            or not _DIGEST.fullmatch(path.stem)
        ):
            raise ValueError(
                f"native fuzz corpus entry {path} is malformed; use <content SHA-256>.input files"
            )
        data = path.read_bytes()
        if len(data) > max_input_size:
            raise ValueError(
                f"native fuzz corpus entry {path} exceeds max_input_size; store an input "
                f"no larger than {max_input_size} bytes"
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != path.stem:
            raise ValueError(
                f"native fuzz corpus entry {path} has the wrong content digest; "
                f"name it {digest}.input"
            )
        digests.append(digest)
    return tuple(digests)


def _stage_inputs(
    target: NativeFuzzTarget, config: NativeCampaignConfig
) -> tuple[Path, Path, tuple[str, ...]]:
    from wreath._fuzz_targets import by_name

    declared = by_name(target.python_target)
    corpus = config.corpus_root / target.name
    initial_digests = _validate_native_corpus(corpus, config.max_input_size)
    for seed in declared.seeds:
        if len(seed) > config.max_input_size:
            raise ValueError(
                f"native fuzz target {target.name} has a {len(seed)}-byte seed; "
                f"use max_input_size of at least {len(seed)} bytes"
            )
        digest = hashlib.sha256(seed).hexdigest()
        _atomic_write(corpus / f"{digest}.input", seed)
    dictionary = config.build_root / "dictionaries" / f"{target.name}.dict"
    lines = []
    for token in declared.dictionary:
        escaped = "".join(
            chr(byte) if 32 <= byte < 127 and byte not in {34, 92} else f"\\x{byte:02x}"
            for byte in token
        )
        lines.append(f'"{escaped}"')
    _atomic_write(dictionary, ("\n".join(lines) + "\n").encode())
    return corpus, dictionary, initial_digests


def _normalize_corpus(corpus: Path, max_input_size: int) -> tuple[str, ...]:
    digests: set[str] = set()
    for path in sorted(corpus.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"native fuzz corpus entry {path} must be a regular file")
        data = path.read_bytes()
        if len(data) > max_input_size:
            raise ValueError(
                f"native fuzz corpus entry {path} exceeds max_input_size; store an input "
                f"no larger than {max_input_size} bytes"
            )
        digest = hashlib.sha256(data).hexdigest()
        canonical = corpus / f"{digest}.input"
        if path == canonical:
            digests.add(digest)
            continue
        if path.suffix == ".input":
            raise ValueError(
                f"native fuzz corpus entry {path} has the wrong content digest; "
                f"name it {canonical.name}"
            )
        if canonical.exists():
            if canonical.is_symlink() or not canonical.is_file() or canonical.read_bytes() != data:
                raise ValueError(f"native fuzz corpus content collision at {canonical}")
        else:
            _atomic_write(canonical, data)
        path.unlink()
        digests.add(digest)
    return tuple(sorted(digests))


def _save_native_finding(
    target: NativeFuzzTarget,
    config: NativeCampaignConfig,
    data: bytes,
    original: bytes,
    diagnostic: bytes,
    source_name: str,
    command: list[str],
    deterministic: bool,
    build_identity: str,
) -> Path:
    from wreath._fuzz_targets import by_name

    declared = by_name(target.python_target)
    digest = hashlib.sha256(data).hexdigest()
    signature = _failure_signature(diagnostic.decode(errors="replace"))
    source_kind = source_name.partition("-")[0]
    if signature is None:
        signature = "libfuzzer", source_kind
    failure_identity = _identity_digest(
        {
            "deterministic": deterministic,
            "failure_signature": list(signature),
            "source_kind": source_kind,
            "target": target.name,
        }
    )
    directory = config.artifact_root / target.name / build_identity / failure_identity
    final = directory / digest
    metadata: dict[str, object] = {
        "backend": "libfuzzer",
        "build_identity": build_identity,
        "campaign_seed": config.seed,
        "command": command,
        "deterministic": deterministic,
        "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
        "diagnostic_size": len(diagnostic),
        "exception_message": source_name,
        "exception_type": "native.libfuzzer",
        "failure_identity": failure_identity,
        "failure_signature": list(signature),
        "input_sha256": digest,
        "minimized_size": len(data),
        "operator_names": list(declared.operator_names),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "original_size": len(original),
        "sanitizers": list(_SANITIZERS),
        "source_kind": source_kind,
        "source_files": list(declared.source_files),
        "target": target.name,
    }
    if final.exists():
        _validate_native_finding(directory, final, target, data, metadata)
        return final
    directory.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=directory))
    try:
        _atomic_write(temporary / "input", data)
        _atomic_write(temporary / "diagnostic.log", diagnostic)
        _atomic_write(
            temporary / "metadata.json",
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(),
        )
        try:
            os.rename(temporary, final)
            _fsync_directory(directory)
        except FileExistsError:
            _validate_native_finding(directory, final, target, data, metadata)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final


def _validate_native_finding(
    directory: Path,
    final: Path,
    target: NativeFuzzTarget,
    data: bytes,
    expected_metadata: Mapping[str, object],
) -> None:
    _validate_artifacts(directory)
    metadata = json.loads((final / "metadata.json").read_text(encoding="utf-8"))
    identity_keys = (
        "backend",
        "build_identity",
        "deterministic",
        "exception_type",
        "failure_identity",
        "failure_signature",
        "input_sha256",
        "sanitizers",
        "source_kind",
        "target",
    )
    stored_identity_values = {key: metadata.get(key) for key in identity_keys}
    expected_identity_values = {key: expected_metadata[key] for key in identity_keys}
    if (
        (final / "input").read_bytes() != data
        or metadata.get("backend") != "libfuzzer"
        or metadata.get("sanitizers") != list(_SANITIZERS)
        or metadata.get("target") != target.name
        or stored_identity_values != expected_identity_values
        or not isinstance(metadata.get("exception_message"), str)
        or not metadata.get("exception_message")
    ):
        raise ValueError(
            f"native fuzz artifact {final} is corrupted or describes another finding or build"
        )


def _last_metric(pattern: str, text: str) -> int:
    matches = re.findall(pattern, text)
    return int(matches[-1]) if matches else 0


def _run_arguments(config: NativeCampaignConfig) -> tuple[str, ...]:
    if config.replay_only:
        return ("-runs=0",)
    if config.max_runs:
        return (f"-runs={config.max_runs}",)
    return ()


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _wall_clock_timeout(seconds: int) -> int:
    return seconds + _WALL_CLOCK_GRACE_SECONDS


def _failure_signature(output: str) -> tuple[str, str] | None:
    match = re.search(r"ERROR: AddressSanitizer:\s*([^\s:]+)", output)
    if match:
        return "address", match.group(1)
    if "runtime error:" in output or "UndefinedBehaviorSanitizer" in output:
        categories = (
            "division by zero",
            "integer overflow",
            "null pointer",
            "out of bounds",
            "shift exponent",
            "signed integer overflow",
            "unsigned integer overflow",
        )
        lowered = output.lower()
        category = next((item for item in categories if item in lowered), "undefined")
        return "undefined", category
    lowered = output.lower()
    if "timeout after" in lowered or "libfuzzer: timeout" in lowered:
        return "libfuzzer", "timeout"
    if "out-of-memory" in lowered or "out of memory" in lowered:
        return "libfuzzer", "out-of-memory"
    if "libfuzzer: deadly signal" in lowered:
        return "libfuzzer", "deadly-signal"
    if "libfuzzer: fuzz target exited" in lowered:
        return "libfuzzer", "target-exited"
    return None


def _replay_finding(
    executable: Path,
    config: NativeCampaignConfig,
    environment: dict[str, str],
    artifact: Path,
) -> tuple[subprocess.CompletedProcess[str], bytes]:
    command = [
        str(executable),
        "-runs=1",
        f"-timeout={config.max_input_seconds}",
        f"-max_len={config.max_input_size}",
        f"-seed={config.seed}",
        str(artifact),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=config.project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=_wall_clock_timeout(config.max_input_seconds),
        )
        diagnostic = result.stdout + result.stderr
    except subprocess.TimeoutExpired as error:
        diagnostic = (
            _timeout_text(error.stdout)
            + _timeout_text(error.stderr)
            + f"\nPython wall-clock timeout after {error.timeout} seconds during replay\n"
        )
        result = subprocess.CompletedProcess(command, 124, "", diagnostic)
    return result, ("\n=== libFuzzer replay ===\n" + diagnostic).encode()


def _compatible_failure(
    expected: tuple[str, str] | None, result: subprocess.CompletedProcess[str]
) -> bool:
    return (
        result.returncode != 0
        and expected is not None
        and _failure_signature(result.stdout + result.stderr) == expected
    )


def _minimize_finding(
    executable: Path,
    config: NativeCampaignConfig,
    environment: dict[str, str],
    artifact: Path,
    campaign_diagnostic: bytes,
) -> tuple[bytes, bytes, bool]:
    original = artifact.read_bytes()
    expected = _failure_signature(campaign_diagnostic.decode(errors="replace"))
    original_replay, original_diagnostic = _replay_finding(
        executable, config, environment, artifact
    )
    original_reproduced = _compatible_failure(expected, original_replay)
    if not original_reproduced:
        return (
            original,
            original_diagnostic + b"original finding did not reproduce compatibly\n",
            False,
        )
    directory = Path(tempfile.mkdtemp(prefix="minimize-", dir=artifact.parent))
    output = directory / "minimized"
    command = [
        str(executable),
        "-minimize_crash=1",
        f"-exact_artifact_path={output}",
        f"-max_total_time={config.max_minimize_seconds}",
        f"-timeout={config.max_input_seconds}",
        f"-max_len={config.max_input_size}",
        f"-seed={config.seed}",
        str(artifact),
    ]
    try:
        try:
            result = subprocess.run(
                command,
                cwd=config.project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=_wall_clock_timeout(config.max_minimize_seconds),
            )
        except subprocess.TimeoutExpired as error:
            timeout_diagnostic = (
                _timeout_text(error.stdout)
                + _timeout_text(error.stderr)
                + f"\nPython wall-clock timeout after {error.timeout} seconds during "
                "minimization\n"
            )
            result = subprocess.CompletedProcess(command, 124, "", timeout_diagnostic)
        diagnostic = (
            original_diagnostic.decode(errors="replace")
            + "\n=== libFuzzer minimization stdout ===\n"
            + result.stdout
            + "\n=== libFuzzer minimization stderr ===\n"
            + result.stderr
        ).encode()
        candidate = output.read_bytes() if output.is_file() else original
        if candidate == original or len(candidate) > len(original):
            return original, diagnostic, True
        candidate_replay, replay_diagnostic = _replay_finding(
            executable, config, environment, output
        )
        if _compatible_failure(expected, candidate_replay):
            repeated_replay, repeated_diagnostic = _replay_finding(
                executable, config, environment, output
            )
            if _compatible_failure(expected, repeated_replay):
                return (
                    candidate,
                    diagnostic + replay_diagnostic + repeated_diagnostic,
                    True,
                )
            return (
                original,
                diagnostic
                + replay_diagnostic
                + repeated_diagnostic
                + b"minimized finding did not reproduce repeatedly; retained original input\n",
                True,
            )
        return (
            original,
            diagnostic
            + replay_diagnostic
            + b"incompatible minimized reproduction; retained original input\n",
            True,
        )
    finally:
        shutil.rmtree(directory)


def run_native_campaign(
    target: NativeFuzzTarget, config: NativeCampaignConfig
) -> NativeCampaignReport:
    executable = (
        build_native_target(target, config)
        if config.rebuild
        else config.build_root / "bin" / target.name
    )
    if not executable.is_file():
        raise ValueError(f"native fuzz executable {executable} is missing; rebuild it")
    build_identity = _validate_build(executable, target, config)
    corpus, dictionary, initial_digests = _stage_inputs(target, config)
    before_digests = set(initial_digests)
    pending_parent = config.build_root / "pending" / target.name
    pending_parent.mkdir(parents=True, exist_ok=True)
    pending = Path(tempfile.mkdtemp(prefix="campaign-", dir=pending_parent))
    command = [
        str(executable),
        str(corpus),
        f"-max_total_time={config.max_seconds}",
        *_run_arguments(config),
        f"-max_len={config.max_input_size}",
        f"-timeout={config.max_input_seconds}",
        f"-seed={config.seed}",
        f"-dict={dictionary}",
        f"-artifact_prefix={pending}/",
        "-print_final_stats=1",
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_target_library(target, config))
    environment["PYTHONHASHSEED"] = str(config.seed)
    environment["ASAN_OPTIONS"] = "detect_leaks=0:abort_on_error=1:symbolize=1"
    environment["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    try:
        campaign_started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=config.project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=_wall_clock_timeout(config.max_seconds),
            )
        except subprocess.TimeoutExpired as error:
            stderr = (
                _timeout_text(error.stderr)
                + f"\nPython wall-clock timeout after {error.timeout} seconds during "
                "campaign\n"
            )
            result = subprocess.CompletedProcess(command, 124, _timeout_text(error.stdout), stderr)
        campaign_elapsed = time.monotonic() - campaign_started
        corpus_digests = _normalize_corpus(corpus, config.max_input_size)
        corpus_addition_digests = tuple(
            digest for digest in corpus_digests if digest not in before_digests
        )
        diagnostic = (
            "=== stdout ===\n" + result.stdout + "\n=== stderr ===\n" + result.stderr
        ).encode()
        saved = []
        for path in sorted(pending.iterdir()):
            if not path.is_file():
                continue
            original = path.read_bytes()
            minimized, minimization_diagnostic, deterministic = _minimize_finding(
                executable, config, environment, path, diagnostic
            )
            saved.append(
                _save_native_finding(
                    target,
                    config,
                    minimized,
                    original,
                    diagnostic + minimization_diagnostic,
                    path.name,
                    command,
                    deterministic,
                    build_identity,
                )
            )
        findings = tuple(saved)
        if result.returncode and not findings:
            raise RuntimeError(
                f"native fuzz harness exited {result.returncode} without a reproduction artifact:\n"
                f"{diagnostic.decode(errors='replace')[-8_000:]}"
            )
        output = result.stdout + result.stderr
        cases = _last_metric(r"stat::number_of_executed_units:\s+(\d+)", output)
        if not cases:
            cases = _last_metric(r"#(\d+)\s+DONE", output)
        return NativeCampaignReport(
            target=target.name,
            seed=config.seed,
            exit_code=result.returncode,
            command=tuple(command),
            findings=findings,
            cases_executed=cases,
            coverage_features=_last_metric(r"\bcov:\s*(\d+)", output),
            fuzzer_features=_last_metric(r"\bft:\s*(\d+)", output),
            peak_rss_mb=_last_metric(r"stat::peak_rss_mb:\s+(\d+)", output),
            corpus_size=len(corpus_digests),
            corpus_added=len(corpus_addition_digests),
            corpus_addition_digests=corpus_addition_digests,
            corpus_digests=corpus_digests,
            stdout=result.stdout,
            stderr=result.stderr,
            elapsed_seconds=campaign_elapsed,
        )
    finally:
        shutil.rmtree(pending)


def merge_native_corpus(
    target: NativeFuzzTarget, config: NativeCampaignConfig
) -> NativeMergeReport:
    executable = (
        build_native_target(target, config)
        if config.rebuild
        else config.build_root / "bin" / target.name
    )
    if not executable.is_file():
        raise ValueError(f"native fuzz executable {executable} is missing; rebuild it")
    _validate_build(executable, target, config)
    corpus, dictionary, _ = _stage_inputs(target, config)
    before_paths = tuple(corpus.iterdir())
    config.corpus_root.mkdir(parents=True, exist_ok=True)
    merged = Path(tempfile.mkdtemp(prefix=f".merge-{target.name}-", dir=config.corpus_root))
    command = [
        str(executable),
        "-merge=1",
        str(merged),
        str(corpus),
        f"-max_total_time={config.max_seconds}",
        f"-max_len={config.max_input_size}",
        f"-timeout={config.max_input_seconds}",
        f"-dict={dictionary}",
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_target_library(target, config))
    environment["PYTHONHASHSEED"] = str(config.seed)
    environment["ASAN_OPTIONS"] = "detect_leaks=0:abort_on_error=1:symbolize=1"
    environment["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    try:
        try:
            result = subprocess.run(
                command,
                cwd=config.project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=_wall_clock_timeout(config.max_seconds),
            )
        except subprocess.TimeoutExpired as error:
            diagnostic = (
                _timeout_text(error.stdout)
                + _timeout_text(error.stderr)
                + f"\nPython wall-clock timeout after {error.timeout} seconds during merge\n"
            )
            raise RuntimeError(
                f"native corpus merge exceeded its wall-clock deadline:\n{diagnostic[-8_000:]}"
            ) from error
        if result.returncode:
            raise RuntimeError(
                f"native corpus merge failed ({result.returncode}):\n"
                f"{(result.stdout + result.stderr)[-8_000:]}"
            )
        _normalize_corpus(merged, config.max_input_size)
        selected = {path.name: path.read_bytes() for path in merged.iterdir()}
        if before_paths and not selected:
            raise RuntimeError("native corpus merge selected no inputs from a non-empty corpus")
        for name, data in selected.items():
            _atomic_write(corpus / name, data)
        for path in before_paths:
            if path.name not in selected:
                path.unlink()
        _fsync_directory(corpus)
        return NativeMergeReport(
            target=target.name,
            before=len(before_paths),
            after=len(selected),
            command=tuple(command),
            stdout=result.stdout,
            stderr=result.stderr,
        )
    finally:
        shutil.rmtree(merged)


__all__ = [
    "NATIVE_TARGETS",
    "NativeCampaignConfig",
    "NativeCampaignReport",
    "NativeFuzzTarget",
    "NativeMergeReport",
    "build_native_target",
    "merge_native_corpus",
    "native_target",
    "run_native_campaign",
]
