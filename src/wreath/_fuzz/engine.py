from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import random
import re
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from .structured import StructuredStrategy

_TARGET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOOL_IDS = (4, 3, 2, 1, 0)
_TOOL_NAME = "wreath-fuzz"
_PACKAGE_DIRECTORY = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class FuzzTarget:
    name: str
    run: Callable[[bytes], Iterable[str] | None]
    seeds: tuple[bytes, ...] = ()
    dictionary: tuple[bytes, ...] = ()
    source_files: tuple[str, ...] = ()
    operator_names: tuple[str, ...] = ()
    strategy: StructuredStrategy | None = None

    def __post_init__(self) -> None:
        if not _TARGET_NAME.fullmatch(self.name):
            raise ValueError(
                f"fuzz target name {self.name!r} is invalid; use lowercase letters, digits, "
                "'.', '_', or '-'"
            )
        if not callable(self.run):
            raise TypeError("fuzz target run must be a callable accepting one bytes input")
        for field_name in ("seeds", "dictionary"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, bytes) for value in values
            ):
                raise TypeError(f"fuzz target {field_name} must be a tuple of bytes")
        for field_name in ("source_files", "operator_names"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
                raise TypeError(f"fuzz target {field_name} must be a tuple of strings")
        if self.strategy is not None and not isinstance(self.strategy, StructuredStrategy):
            raise TypeError("fuzz target strategy must be a StructuredStrategy or None")


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    corpus_root: Path
    artifact_root: Path
    journal_path: Path | None = None
    seed: int | None = None
    max_cases: int = 10_000
    max_seconds: float = 60.0
    max_input_size: int = 65_536
    max_shrink_cases: int = 1_024
    max_findings: int = 16
    generate: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "corpus_root", Path(self.corpus_root))
        object.__setattr__(self, "artifact_root", Path(self.artifact_root))
        if self.journal_path is not None:
            object.__setattr__(self, "journal_path", Path(self.journal_path))
            if self.journal_path.is_dir():
                raise ValueError("journal_path must be a file path, not a directory")
        for name in ("max_cases", "max_input_size", "max_shrink_cases", "max_findings"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.max_seconds, bool) or not isinstance(self.max_seconds, int | float):
            raise ValueError("max_seconds must be positive")
        if not math.isfinite(self.max_seconds) or self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**64
        ):
            raise ValueError("seed must be None or an integer from 0 through 2**64 - 1")
        if not isinstance(self.generate, bool):
            raise ValueError("generate must be a boolean")


@dataclass(frozen=True, slots=True)
class Finding:
    digest: str
    deterministic: bool
    exception_type: str
    exception_message: str
    original_size: int
    minimized_size: int
    input_path: Path
    metadata_path: Path


@dataclass(frozen=True, slots=True)
class CampaignReport:
    target: str
    seed: int
    cases_executed: int
    corpus_size: int
    corpus_added: int
    coverage_features: int
    semantic_features: int
    generated_digests: tuple[str, ...]
    initial_corpus_digests: tuple[str, ...]
    corpus_digests: tuple[str, ...]
    findings: tuple[Finding, ...]
    stop_reason: str
    elapsed_seconds: float
    structured_strategy: str | None = None


@dataclass(frozen=True, slots=True)
class CorpusPruneReport:
    target: str
    before: int
    after: int
    removed: int
    coverage_features: int
    semantic_features: int


@dataclass(slots=True)
class _ObservedFailure:
    exception_type: str
    message: str


@dataclass(slots=True)
class _ExceptionTrap:
    error: Exception | None = None

    def __enter__(self) -> _ExceptionTrap:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        if exception is None:
            return False
        if not isinstance(exception, Exception):
            return False
        self.error = exception
        return True


class _Coverage:
    def __init__(self, source_files: frozenset[str]) -> None:
        self._source_files = source_files
        self._active = False
        self._current: set[tuple[str, int, int]] = set()
        self._file_cache: dict[Any, str | None] = {}
        self._tool_id: int | None = None

    def _filename(self, code: Any) -> str | None:
        if code not in self._file_cache:
            filename = os.path.realpath(code.co_filename)
            self._file_cache[code] = filename if filename in self._source_files else None
        return self._file_cache[code]

    def _line(self, code: Any, line: int) -> Any:
        filename = self._filename(code)
        if filename is None:
            return sys.monitoring.DISABLE
        if self._active:
            self._current.add((filename, -1, line))
        return None

    def _branch(self, code: Any, source: int, destination: int) -> Any:
        filename = self._filename(code)
        if filename is None:
            return sys.monitoring.DISABLE
        if self._active:
            self._current.add((filename, source, destination))
        return None

    def start(self) -> None:
        if not self._source_files:
            return
        monitoring = sys.monitoring
        tool_id = next((value for value in _TOOL_IDS if monitoring.get_tool(value) is None), None)
        if tool_id is None:
            raise RuntimeError("no PEP 669 monitoring tool slot is available for fuzz coverage")
        monitoring.use_tool_id(tool_id, _TOOL_NAME)
        monitoring.register_callback(tool_id, monitoring.events.LINE, self._line)
        monitoring.register_callback(tool_id, monitoring.events.BRANCH, self._branch)
        monitoring.set_events(tool_id, monitoring.events.LINE | monitoring.events.BRANCH)
        self._tool_id = tool_id

    def begin(self) -> None:
        self._current.clear()
        self._active = True

    def end(self) -> frozenset[tuple[str, int, int]]:
        self._active = False
        return frozenset(self._current)

    def stop(self) -> None:
        tool_id = self._tool_id
        if tool_id is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(tool_id, 0)
        monitoring.register_callback(tool_id, monitoring.events.LINE, None)
        monitoring.register_callback(tool_id, monitoring.events.BRANCH, None)
        monitoring.free_tool_id(tool_id)
        self._tool_id = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_corpus(directory: Path, max_input_size: int) -> dict[str, bytes]:
    directory.mkdir(parents=True, exist_ok=True)
    entries: dict[str, bytes] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".input" or not _DIGEST.fullmatch(path.stem):
            raise ValueError(
                f"corpus entry {path} is malformed; use <content SHA-256>.input files"
            )
        data = path.read_bytes()
        if len(data) > max_input_size:
            raise ValueError(
                f"corpus entry {path} exceeds max_input_size; store an input no larger than "
                f"{max_input_size} bytes"
            )
        digest = _sha256(data)
        if digest != path.stem:
            raise ValueError(
                f"corpus entry {path} does not match its SHA-256; name it {digest}.input"
            )
        entries[digest] = data
    return entries


def _validate_artifacts(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".tmp-"):
            continue
        if not path.is_dir() or not _DIGEST.fullmatch(path.name):
            raise ValueError(f"artifact {path} is malformed; use a SHA-256 directory")
        input_path = path / "input"
        metadata_path = path / "metadata.json"
        if not input_path.is_file() or not metadata_path.is_file():
            raise ValueError(f"artifact {path} is malformed; input and metadata.json are required")
        data = input_path.read_bytes()
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"artifact {path} has malformed metadata.json; use a JSON object"
            ) from error
        digest = _sha256(data)
        if (
            not isinstance(metadata, dict)
            or digest != path.name
            or metadata.get("input_sha256") != digest
        ):
            raise ValueError(f"artifact {path} does not match its input SHA-256 metadata")
        diagnostic_path = path / "diagnostic.log"
        diagnostic_digest = metadata.get("diagnostic_sha256")
        if diagnostic_digest is None:
            if diagnostic_path.exists():
                raise ValueError(f"artifact {path} has a diagnostic without SHA-256 metadata")
        elif (
            not isinstance(diagnostic_digest, str)
            or not diagnostic_path.is_file()
            or _sha256(diagnostic_path.read_bytes()) != diagnostic_digest
        ):
            raise ValueError(f"artifact {path} does not match its diagnostic SHA-256 metadata")


def _validate_finding(path: Path, data: bytes, expected: dict[str, Any]) -> None:
    input_path = path / "input"
    metadata_path = path / "metadata.json"
    if not input_path.is_file() or not metadata_path.is_file():
        raise ValueError(f"artifact {path} is malformed; input and metadata.json are required")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"artifact {path} has malformed metadata.json; use a JSON object"
        ) from error
    identity_fields = (
        "deterministic",
        "exception_message",
        "exception_type",
        "input_sha256",
        "minimized_size",
        "operator_names",
        "source_files",
        "target",
    )
    if "diagnostic_sha256" in expected:
        identity_fields += ("diagnostic_sha256", "diagnostic_size")
    if "structured_strategy" in expected:
        identity_fields += ("structured_strategy",)
    if input_path.read_bytes() != data or not isinstance(metadata, dict) or any(
        metadata.get(field) != expected[field] for field in identity_fields
    ):
        raise ValueError(f"artifact {path} does not describe this finding")


def _source_files(target: FuzzTarget) -> frozenset[str]:
    paths = target.source_files
    if not paths:
        source = inspect.getsourcefile(inspect.unwrap(target.run))
        paths = () if source is None else (source,)
    return frozenset(_resolve_source_file(path) for path in paths if _is_python_source(path))


def _is_python_source(path: str) -> bool:
    return Path(path).suffix in {".py", ".pyw"}


def _resolve_source_file(path: str) -> str:
    source = Path(path)
    if source.is_absolute():
        return os.path.realpath(source)
    parts = source.parts
    if len(parts) >= 2 and parts[:2] == ("src", "wreath"):
        source = _PACKAGE_DIRECTORY.joinpath(*parts[2:])
    elif parts and parts[0] == "wreath":
        source = _PACKAGE_DIRECTORY.joinpath(*parts[1:])
    else:
        package_parent = _PACKAGE_DIRECTORY.parent
        project = package_parent.parent if package_parent.name == "src" else package_parent
        source = project / source
    return os.path.realpath(source)


def _semantic_features(result: Iterable[str] | None) -> frozenset[str]:
    if result is None:
        return frozenset()
    if isinstance(result, str | bytes):
        raise TypeError("fuzz target result must be None or an iterable of feature strings")
    features = frozenset(result)
    if any(not isinstance(feature, str) for feature in features):
        raise TypeError("fuzz target result must contain only feature strings")
    return features


def _exception_type(error: Exception) -> str:
    kind = type(error)
    return f"{kind.__module__}.{kind.__qualname__}"


def _invoke(
    target: FuzzTarget,
    coverage: _Coverage,
    data: bytes,
) -> tuple[frozenset[tuple[str, int, int]], frozenset[str], _ObservedFailure | None]:
    coverage.begin()
    trap = _ExceptionTrap()
    with trap:
        result = target.run(data)
    lines = coverage.end()
    if trap.error is not None:
        return lines, frozenset(), _ObservedFailure(
            _exception_type(trap.error), str(trap.error)
        )
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise TypeError("fuzz target run must be synchronous; return semantic features directly")
    return lines, _semantic_features(result), None


def _same_failure(
    target: FuzzTarget,
    data: bytes,
    wanted: _ObservedFailure,
    journal_path: Path | None,
    campaign_seed: int,
    case_ordinal: int,
) -> bool:
    if journal_path is not None:
        _write_journal(journal_path, target, campaign_seed, case_ordinal, data)
    trap = _ExceptionTrap()
    with trap:
        result = target.run(data)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
    if journal_path is not None:
        journal_path.unlink(missing_ok=True)
    error = trap.error
    return (
        error is not None
        and _exception_type(error) == wanted.exception_type
        and str(error) == wanted.message
    )


def _shrink(
    target: FuzzTarget,
    data: bytes,
    failure: _ObservedFailure,
    max_cases: int,
    deadline: float,
    journal_path: Path | None,
    campaign_seed: int,
    case_ordinal: int,
) -> bytes:
    current = data
    attempts = 0
    if target.strategy is not None:
        candidates = target.strategy.shrink_cases(
            current,
            max_candidates=max_cases,
            max_size=len(current),
        )
        for candidate in candidates:
            if attempts >= max_cases or time.monotonic() >= deadline:
                break
            attempts += 1
            if _same_failure(
                target,
                candidate,
                failure,
                journal_path,
                campaign_seed,
                case_ordinal,
            ):
                current = candidate
                break
    partitions = 2
    while current and attempts < max_cases and time.monotonic() < deadline:
        width = math.ceil(len(current) / partitions)
        reduced = False
        for start in range(0, len(current), width):
            if attempts >= max_cases or time.monotonic() >= deadline:
                break
            candidate = current[:start] + current[start + width :]
            attempts += 1
            if _same_failure(
                target,
                candidate,
                failure,
                journal_path,
                campaign_seed,
                case_ordinal,
            ):
                current = candidate
                partitions = max(2, partitions - 1)
                reduced = True
                break
        if reduced:
            continue
        if partitions >= len(current):
            break
        partitions = min(len(current), partitions * 2)
    for index in range(len(current)):
        if attempts >= max_cases or time.monotonic() >= deadline:
            break
        for replacement in (0, 10, 13, 32, 48, 65, 97, 255):
            if current[index] == replacement:
                continue
            candidate = current[:index] + bytes((replacement,)) + current[index + 1 :]
            attempts += 1
            if _same_failure(
                target,
                candidate,
                failure,
                journal_path,
                campaign_seed,
                case_ordinal,
            ):
                current = candidate
                break
            if attempts >= max_cases or time.monotonic() >= deadline:
                break
    return current


def _mutate(
    rng: random.Random,
    corpus: list[bytes],
    dictionary: tuple[bytes, ...],
    max_size: int,
) -> bytes:
    base = bytearray(rng.choice(corpus))
    operation = rng.randrange(6)
    if not base:
        operation = 2
    if operation == 0:
        index = rng.randrange(len(base))
        base[index] ^= 1 << rng.randrange(8)
    elif operation == 1:
        base[rng.randrange(len(base))] = rng.randrange(256)
    elif operation == 2:
        token = (
            rng.choice(dictionary)
            if dictionary and rng.randrange(2)
            else bytes((rng.randrange(256),))
        )
        index = rng.randrange(len(base) + 1)
        base[index:index] = token
    elif operation == 3:
        start = rng.randrange(len(base))
        stop = rng.randrange(start + 1, len(base) + 1)
        del base[start:stop]
    elif operation == 4:
        other = rng.choice(corpus)
        left = rng.randrange(len(base) + 1)
        right = rng.randrange(len(other) + 1)
        base = base[:left] + other[right:]
    else:
        first = rng.randrange(len(base))
        second = rng.randrange(len(base))
        base[first], base[second] = base[second], base[first]
    return bytes(base[:max_size])


def _mutate_target(
    target: FuzzTarget,
    rng: random.Random,
    corpus: list[bytes],
    max_size: int,
) -> bytes:
    strategy = target.strategy
    if strategy is None:
        return _mutate(rng, corpus, target.dictionary, max_size)
    operation = rng.randrange(4)
    base = rng.choice(corpus)
    if operation == 0:
        candidate = strategy.generate_case(rng, max_size)
    elif operation == 1:
        candidate = strategy.mutate_case(base, rng, max_size)
    elif operation == 2:
        candidate = strategy.crossover_case(base, rng.choice(corpus), rng, max_size)
    else:
        candidate = None
    if candidate is not None:
        return candidate
    tokens = strategy.dictionary_tokens(
        base,
        max_tokens=128,
        max_token_size=min(256, max_size),
    )
    dictionary = tuple(dict.fromkeys((*target.dictionary, *tokens)))
    return _mutate(rng, corpus, dictionary, max_size)


def _save_finding(
    directory: Path,
    target: FuzzTarget,
    seed: int,
    original: bytes,
    minimized: bytes,
    failure: _ObservedFailure,
    deterministic: bool,
    diagnostic: bytes | None = None,
) -> Finding:
    digest = _sha256(minimized)
    final = directory / digest
    metadata = {
        "campaign_seed": seed,
        "deterministic": deterministic,
        "exception_message": failure.message,
        "exception_type": failure.exception_type,
        "input_sha256": digest,
        "minimized_size": len(minimized),
        "operator_names": list(target.operator_names),
        "original_sha256": _sha256(original),
        "original_size": len(original),
        "source_files": list(target.source_files),
        "target": target.name,
    }
    if diagnostic:
        metadata["diagnostic_sha256"] = _sha256(diagnostic)
        metadata["diagnostic_size"] = len(diagnostic)
    if target.strategy is not None:
        metadata["structured_strategy"] = target.strategy.identity
    if final.exists():
        _validate_finding(final, minimized, metadata)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=directory))
        try:
            _atomic_write(temporary / "input", minimized)
            if diagnostic:
                _atomic_write(temporary / "diagnostic.log", diagnostic)
            _atomic_write(
                temporary / "metadata.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(),
            )
            try:
                os.replace(temporary, final)
                _fsync_directory(directory)
            except FileExistsError:
                _validate_finding(final, minimized, metadata)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return Finding(
        digest=digest,
        deterministic=deterministic,
        exception_type=failure.exception_type,
        exception_message=failure.message,
        original_size=len(original),
        minimized_size=len(minimized),
        input_path=final / "input",
        metadata_path=final / "metadata.json",
    )


def _write_journal(
    path: Path,
    target: FuzzTarget,
    seed: int,
    case_ordinal: int,
    data: bytes,
) -> None:
    payload = {
        "campaign_seed": seed,
        "case_ordinal": case_ordinal,
        "input_hex": data.hex(),
        "input_sha256": _sha256(data),
        "target": target.name,
        "version": 1,
    }
    _atomic_write(
        path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def _load_journal(
    path: Path,
    target: FuzzTarget,
    expected_seed: int,
) -> bytes:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"fuzz crash journal {path} must contain one JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError(f"fuzz crash journal {path} must contain one JSON object")
    if payload.get("version") != 1:
        raise ValueError(f"fuzz crash journal {path} has an unsupported version; expected 1")
    if payload.get("target") != target.name:
        raise ValueError(
            f"fuzz crash journal {path} names target {payload.get('target')!r}; "
            f"expected {target.name!r}"
        )
    seed = payload.get("campaign_seed")
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed < 2**64
        or seed != expected_seed
    ):
        raise ValueError(
            f"fuzz crash journal {path} has campaign seed {seed!r}; expected {expected_seed}"
        )
    ordinal = payload.get("case_ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError(
            f"fuzz crash journal {path} has invalid case_ordinal; expected a non-negative integer"
        )
    encoded = payload.get("input_hex")
    if not isinstance(encoded, str):
        raise ValueError(f"fuzz crash journal {path} has invalid input_hex; expected hexadecimal")
    try:
        data = bytes.fromhex(encoded)
    except ValueError as error:
        raise ValueError(
            f"fuzz crash journal {path} has invalid input_hex; expected hexadecimal"
        ) from error
    digest = _sha256(data)
    if payload.get("input_sha256") != digest:
        raise ValueError(
            f"fuzz crash journal {path} does not match its input SHA-256; expected {digest}"
        )
    return data


def publish_crash_finding(
    journal_path: Path,
    artifact_root: Path,
    target: FuzzTarget,
    expected_seed: int,
    *,
    exception_type: str,
    exception_message: str,
    diagnostic: bytes | Path | None = None,
) -> Finding:
    if not isinstance(exception_type, str) or not exception_type:
        raise ValueError("exception_type must be a non-empty signal identifier")
    if not isinstance(exception_message, str) or not exception_message:
        raise ValueError("exception_message must be a non-empty signal description")
    if (
        isinstance(expected_seed, bool)
        or not isinstance(expected_seed, int)
        or not 0 <= expected_seed < 2**64
    ):
        raise ValueError("expected_seed must be an integer from 0 through 2**64 - 1")
    journal = Path(journal_path)
    data = _load_journal(journal, target, expected_seed)
    diagnostic_bytes = diagnostic.read_bytes() if isinstance(diagnostic, Path) else diagnostic
    if diagnostic_bytes is not None and not isinstance(diagnostic_bytes, bytes):
        raise TypeError("diagnostic must be bytes, a Path, or None")
    artifact_directory = Path(artifact_root) / target.name
    _validate_artifacts(artifact_directory)
    finding = _save_finding(
        artifact_directory,
        target,
        expected_seed,
        data,
        data,
        _ObservedFailure(exception_type, exception_message),
        False,
        diagnostic_bytes,
    )
    journal.unlink(missing_ok=True)
    return finding


def prune_corpus(
    target: FuzzTarget,
    corpus_root: Path,
    *,
    max_input_size: int = 65_536,
) -> CorpusPruneReport:
    if (
        isinstance(max_input_size, bool)
        or not isinstance(max_input_size, int)
        or max_input_size <= 0
    ):
        raise ValueError("max_input_size must be a positive integer")
    directory = Path(corpus_root) / target.name
    corpus = _load_corpus(directory, max_input_size)
    ordered = sorted(corpus.items(), key=lambda item: (len(item[1]), item[0]))
    retained: set[str] = set()
    known_coverage: set[tuple[str, int, int]] = set()
    known_semantic: set[str] = set()
    known_failures: set[tuple[str, str]] = set()
    coverage = _Coverage(_source_files(target))
    coverage.start()
    try:
        for digest, data in ordered:
            lines, semantic, failure = _invoke(target, coverage, data)
            failure_key = (
                None
                if failure is None
                else (failure.exception_type, failure.message)
            )
            novel = bool(lines - known_coverage or semantic - known_semantic)
            if failure_key is not None and failure_key not in known_failures:
                novel = True
            if novel or not retained:
                retained.add(digest)
                known_coverage.update(lines)
                known_semantic.update(semantic)
                if failure_key is not None:
                    known_failures.add(failure_key)
    finally:
        coverage.stop()
    for digest in corpus.keys() - retained:
        (directory / f"{digest}.input").unlink()
    if len(retained) != len(corpus):
        _fsync_directory(directory)
    return CorpusPruneReport(
        target.name,
        len(corpus),
        len(retained),
        len(corpus) - len(retained),
        len(known_coverage),
        len(known_semantic),
    )


def run_campaign(target: FuzzTarget, config: CampaignConfig) -> CampaignReport:
    corpus_directory = config.corpus_root / target.name
    artifact_directory = config.artifact_root / target.name
    corpus = _load_corpus(corpus_directory, config.max_input_size)
    initial_corpus_digests = tuple(sorted(corpus))
    _validate_artifacts(artifact_directory)
    campaign_seed = config.seed if config.seed is not None else secrets.randbits(64)
    rng = random.Random(campaign_seed)
    strategy_seeds = () if target.strategy is None else target.strategy.seeds
    initial = [
        data[: config.max_input_size]
        for data in (*target.seeds, *strategy_seeds, *corpus.values())
    ]
    if config.generate and target.strategy is not None:
        generated = target.strategy.generate_case(rng, config.max_input_size)
        if generated is not None:
            initial.append(generated)
    if not initial:
        initial = [b""]
    queue = list(dict.fromkeys(initial))
    initial_digests = {_sha256(value) for value in queue}
    mutation_pool = list(queue)
    mutation_pool_digests = set(initial_digests)
    known_coverage: set[tuple[str, int, int]] = set()
    known_semantic: set[str] = set()
    findings: list[Finding] = []
    finding_digests: set[str] = set()
    generated_digests: list[str] = []
    corpus_added = 0
    cases = 0
    started = time.monotonic()
    deadline = started + config.max_seconds
    stop_reason = "case-limit"
    coverage = _Coverage(_source_files(target))
    coverage.start()
    try:
        while cases < config.max_cases:
            if time.monotonic() >= deadline:
                stop_reason = "time-limit"
                break
            if len(findings) >= config.max_findings:
                stop_reason = "finding-limit"
                break
            if cases >= len(queue) and not config.generate:
                stop_reason = "corpus-exhausted"
                break
            data = (
                queue[cases]
                if cases < len(queue)
                else _mutate_target(target, rng, mutation_pool, config.max_input_size)
            )
            case_ordinal = cases
            digest = _sha256(data)
            generated_digests.append(digest)
            if config.journal_path is not None:
                _write_journal(
                    config.journal_path,
                    target,
                    campaign_seed,
                    case_ordinal,
                    data,
                )
            lines, semantic, failure = _invoke(target, coverage, data)
            if config.journal_path is not None:
                config.journal_path.unlink(missing_ok=True)
            cases += 1
            novel = bool(lines - known_coverage or semantic - known_semantic)
            known_coverage.update(lines)
            known_semantic.update(semantic)
            if novel or digest in initial_digests:
                if digest not in corpus:
                    _atomic_write(corpus_directory / f"{digest}.input", data)
                    corpus[digest] = data
                    corpus_added += 1
                if digest not in mutation_pool_digests:
                    mutation_pool.append(data)
                    mutation_pool_digests.add(digest)
            if failure is None:
                continue
            deterministic = time.monotonic() < deadline and _same_failure(
                target,
                data,
                failure,
                config.journal_path,
                campaign_seed,
                case_ordinal,
            )
            minimized = (
                _shrink(
                    target,
                    data,
                    failure,
                    config.max_shrink_cases,
                    deadline,
                    config.journal_path,
                    campaign_seed,
                    case_ordinal,
                )
                if deterministic
                else data
            )
            finding = _save_finding(
                artifact_directory,
                target,
                campaign_seed,
                data,
                minimized,
                failure,
                deterministic,
            )
            if finding.digest not in finding_digests:
                findings.append(finding)
                finding_digests.add(finding.digest)
    finally:
        coverage.stop()
    elapsed = time.monotonic() - started
    return CampaignReport(
        target=target.name,
        seed=campaign_seed,
        cases_executed=cases,
        corpus_size=len(corpus),
        corpus_added=corpus_added,
        coverage_features=len(known_coverage),
        semantic_features=len(known_semantic),
        generated_digests=tuple(generated_digests),
        initial_corpus_digests=initial_corpus_digests,
        corpus_digests=tuple(sorted(corpus)),
        findings=tuple(findings),
        stop_reason=stop_reason,
        elapsed_seconds=elapsed,
        structured_strategy=(target.strategy.identity if target.strategy is not None else None),
    )
