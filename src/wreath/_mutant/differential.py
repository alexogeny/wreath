from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import signal
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from wreath._fuzz import CampaignConfig, FuzzTarget, publish_crash_finding, run_campaign

from .model import Outcome, Report, Verdict


class MutantSemanticDivergence(AssertionError):
    pass


@dataclass(frozen=True, slots=True)
class DifferentialFuzzConfig:
    corpus_root: Path
    artifact_root: Path
    seed: int | None = None
    max_cases: int = 1_000
    max_seconds: float = 10.0
    target_names: tuple[str, ...] = ()
    targets: tuple[FuzzTarget, ...] = ()
    generate: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "corpus_root", Path(self.corpus_root))
        object.__setattr__(self, "artifact_root", Path(self.artifact_root))
        CampaignConfig(
            self.corpus_root,
            self.artifact_root,
            seed=self.seed,
            max_cases=self.max_cases,
            max_seconds=self.max_seconds,
            generate=self.generate,
        )
        if self.target_names:
            if self.targets:
                available = {target.name for target in self.targets}
            else:
                from wreath._fuzz_targets import TARGETS

                available = {target.name for target in TARGETS}
            unknown = sorted(set(self.target_names) - available)
            if unknown:
                choices = ", ".join(sorted(available))
                raise ValueError(
                    f"unknown differential fuzz target {unknown[0]!r}; choose one of: {choices}"
                )


@dataclass(slots=True)
class _ObservationTrap:
    error: Exception | None = None

    def __enter__(self) -> _ObservationTrap:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        if exception is None or not isinstance(exception, Exception):
            return False
        self.error = exception
        return True


def _observe(target: FuzzTarget, data: bytes) -> tuple[str, ...]:
    trap = _ObservationTrap()
    with trap:
        result = target.run(data)
    if trap.error is not None:
        kind = type(trap.error)
        return ("exception", f"{kind.__module__}.{kind.__qualname__}", str(trap.error))
    if result is None:
        return ("return",)
    if isinstance(result, str | bytes):
        raise TypeError("differential fuzz target must return feature strings, not text or bytes")
    features = tuple(result)
    if any(not isinstance(feature, str) for feature in features):
        raise TypeError("differential fuzz target features must all be strings")
    return ("return", *sorted(set(features)))


def _isolated_observe(
    target: FuzzTarget,
    data: bytes,
    patch: Any | None,
) -> tuple[str, ...]:
    read_descriptor, write_descriptor = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_descriptor)
        exit_code = 1
        try:
            if patch is not None:
                patch.apply()
            payload = json.dumps({"observation": _observe(target, data)}).encode()
            view = memoryview(payload)
            while view:
                written = os.write(write_descriptor, view)
                view = view[written:]
            exit_code = 0
        finally:
            os.close(write_descriptor)
            os._exit(exit_code)
    os.close(write_descriptor)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(read_descriptor, 65_536):
            chunks.append(chunk)
    finally:
        os.close(read_descriptor)
    _, status = os.waitpid(pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    if exit_code < 0:
        os.kill(os.getpid(), -exit_code)
        raise RuntimeError("differential observation signal propagation returned unexpectedly")
    if exit_code:
        os._exit(exit_code)
    try:
        payload = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("differential observation worker returned malformed evidence") from error
    observation = payload.get("observation")
    if not isinstance(observation, list) or any(
        not isinstance(value, str) for value in observation
    ):
        raise RuntimeError("differential observation worker returned malformed evidence")
    return tuple(observation)


def _differential_target(target: FuzzTarget, verdict: Verdict) -> FuzzTarget:
    mutation = verdict.mutation
    patch = mutation.patch
    if patch is None:
        raise ValueError(f"{mutation.identifier} has no live patch")

    def run(data: bytes) -> Iterable[str]:
        pristine = _isolated_observe(target, data, None)
        changed = _isolated_observe(target, data, patch)
        if pristine != changed:
            raise MutantSemanticDivergence(
                f"mutant semantic features or exception distinguished: {mutation.identifier}"
            )
        return (f"differential:{pristine[0]}", *pristine[1:])

    return FuzzTarget(
        target.name,
        run,
        seeds=target.seeds,
        dictionary=target.dictionary,
        source_files=target.source_files,
        operator_names=target.operator_names,
        strategy=target.strategy,
    )


def _pair_seed(master_seed: int, identifier: str, target: str) -> int:
    material = f"{master_seed}\0{identifier}\0{target}".encode()
    return int.from_bytes(hashlib.blake2b(material, digest_size=8).digest())


def _artifact_namespace(identifier: str) -> str:
    return hashlib.sha256(identifier.encode()).hexdigest()


def _targets_for(verdict: Verdict, config: DifferentialFuzzConfig) -> tuple[FuzzTarget, ...]:
    if config.targets:
        available = config.targets
    else:
        from wreath._fuzz_targets import TARGETS

        available = TARGETS
    selected_names = frozenset(config.target_names)
    mutation = verdict.mutation
    return tuple(
        target
        for target in available
        if (not selected_names or target.name in selected_names)
        and mutation.site.path in target.source_files
        and mutation.operator in target.operator_names
    )


def _run_probe(
    verdict: Verdict,
    target: FuzzTarget,
    campaign: CampaignConfig,
    result_path: Path,
    deadline: float,
) -> None:
    result_path.unlink(missing_ok=True)
    diagnostic_path = result_path.with_suffix(".stderr")
    diagnostic_path.unlink(missing_ok=True)
    pid = os.fork()
    if pid == 0:
        diagnostic_fd = os.open(
            diagnostic_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        os.dup2(diagnostic_fd, 2)
        os.close(diagnostic_fd)
        try:
            report = run_campaign(_differential_target(target, verdict), campaign)
            payload = {
                "cases": report.cases_executed,
                "coverage_features": report.coverage_features,
                "semantic_features": report.semantic_features,
                "seed": report.seed,
                "stop_reason": report.stop_reason,
                "findings": [
                    {
                        "digest": finding.digest,
                        "deterministic": finding.deterministic,
                        "exception_message": finding.exception_message,
                        "exception_type": finding.exception_type,
                        "input_path": str(finding.input_path),
                        "metadata_path": str(finding.metadata_path),
                    }
                    for finding in report.findings
                ],
            }
            result_path.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            result_path.write_text(
                json.dumps({"error": f"{type(error).__name__}: {error}"}),
                encoding="utf-8",
            )
            os._exit(1)
        os._exit(0)
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            break
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            _record_worker_failure(
                result_path,
                diagnostic_path,
                campaign,
                target,
                "timeout",
                "differential fuzz worker exceeded its allotted wall time",
            )
            return
        time.sleep(0.005)
    exit_code = os.waitstatus_to_exitcode(status)
    if exit_code != 0:
        if os.WIFSIGNALED(status):
            number = os.WTERMSIG(status)
            kind = "signal"
            message = f"differential fuzz worker died from signal {number}"
        else:
            kind = "worker-exit"
            message = f"differential fuzz worker exited {exit_code}"
        _record_worker_failure(
            result_path,
            diagnostic_path,
            campaign,
            target,
            kind,
            message,
        )
        return
    diagnostic_path.unlink(missing_ok=True)
    if not result_path.exists():
        raise RuntimeError("differential fuzz worker exited successfully without a result")


def _record_worker_failure(
    result_path: Path,
    diagnostic_path: Path,
    campaign: CampaignConfig,
    target: FuzzTarget,
    kind: str,
    message: str,
) -> None:
    payload: dict[str, Any]
    if result_path.exists():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            payload = {"worker_payload_error": f"{type(error).__name__}: {error}"}
        else:
            payload = (
                loaded
                if isinstance(loaded, dict)
                else {"worker_payload_error": "not an object"}
            )
    else:
        payload = {}
    payload.update({"worker_failure": kind, "worker_message": message, "stop_reason": kind})
    journal_path = campaign.journal_path
    if journal_path is not None and journal_path.is_file():
        try:
            finding = publish_crash_finding(
                journal_path,
                campaign.artifact_root,
                target,
                campaign.seed if campaign.seed is not None else 0,
                exception_type=kind,
                exception_message=message,
                diagnostic=diagnostic_path if diagnostic_path.is_file() else None,
            )
        except (OSError, TypeError, ValueError) as error:
            payload["crash_artifact_error"] = f"{type(error).__name__}: {error}"
        else:
            payload["cases"] = max(1, int(payload.get("cases", 0)))
            payload["crash_finding"] = {
                "digest": finding.digest,
                "deterministic": finding.deterministic,
                "exception_type": finding.exception_type,
                "input_path": str(finding.input_path),
                "metadata_path": str(finding.metadata_path),
            }
    diagnostic_path.unlink(missing_ok=True)
    result_path.write_text(json.dumps(payload), encoding="utf-8")


def apply_differential_fuzz(
    report: Report,
    config: DifferentialFuzzConfig,
    *,
    workdir: Path,
) -> None:
    master_seed = config.seed if config.seed is not None else secrets.randbits(64)
    started = time.monotonic()
    deadline = started + config.max_seconds
    remaining_cases = config.max_cases
    probes = 0
    evidence: list[dict[str, Any]] = []
    pairs = [
        (ordinal, verdict, target)
        for ordinal, verdict in enumerate(report.verdicts)
        if verdict.outcome is Outcome.SURVIVED
        for target in _targets_for(verdict, config)
    ]
    for pair_index, (ordinal, verdict, target) in enumerate(pairs):
        if verdict.outcome is not Outcome.SURVIVED:
            continue
        remaining_seconds = deadline - time.monotonic()
        if remaining_cases <= 0 or remaining_seconds <= 0:
            break
        remaining_pairs = sum(
            1
            for _, candidate, _ in pairs[pair_index:]
            if candidate.outcome is Outcome.SURVIVED
        )
        case_share = math.ceil(remaining_cases / remaining_pairs)
        second_share = remaining_seconds / remaining_pairs
        pair_deadline = min(deadline, time.monotonic() + second_share)
        pair_seed = _pair_seed(master_seed, verdict.mutation.identifier, target.name)
        namespace = _artifact_namespace(verdict.mutation.identifier)
        campaign = CampaignConfig(
            config.corpus_root,
            config.artifact_root / "mutants" / namespace,
            journal_path=workdir / f"differential-{ordinal}-{probes}.journal",
            seed=pair_seed,
            max_cases=case_share,
            max_seconds=second_share,
            max_input_size=65_536,
            max_shrink_cases=min(1_024, case_share),
            max_findings=1,
            generate=config.generate,
        )
        result_path = workdir / f"differential-{ordinal}-{probes}.json"
        _run_probe(verdict, target, campaign, result_path, pair_deadline)
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        finally:
            result_path.unlink(missing_ok=True)
        probes += 1
        cases = int(payload.get("cases", 0))
        remaining_cases -= cases
        record = {
            "target": target.name,
            "seed": pair_seed,
            "cases": cases,
            "coverage_features": int(payload.get("coverage_features", 0)),
            "semantic_features": int(payload.get("semantic_features", 0)),
            "comparison": "semantic-features-and-exception",
            "stop_reason": payload.get("stop_reason", "error"),
        }
        if "worker_failure" in payload:
            record["worker_failure"] = payload["worker_failure"]
            record["worker_message"] = payload["worker_message"]
            if "error" in payload:
                record["error"] = payload["error"]
            if "worker_payload_error" in payload:
                record["worker_payload_error"] = payload["worker_payload_error"]
            if "crash_finding" in payload:
                record["crash_finding"] = payload["crash_finding"]
            if "crash_artifact_error" in payload:
                record["crash_artifact_error"] = payload["crash_artifact_error"]
            evidence.append(record)
            verdict.fuzz_evidence = (*verdict.fuzz_evidence, record)
            continue
        if "error" in payload:
            record["error"] = payload["error"]
            evidence.append(record)
            verdict.fuzz_evidence = (*verdict.fuzz_evidence, record)
            continue
        if payload.get("timeout"):
            record["timeout"] = True
            evidence.append(record)
            verdict.fuzz_evidence = (*verdict.fuzz_evidence, record)
            continue
        divergences = [
            finding
            for finding in payload["findings"]
            if finding["deterministic"]
            and finding["exception_type"]
            == (
                f"{MutantSemanticDivergence.__module__}."
                f"{MutantSemanticDivergence.__qualname__}"
            )
        ]
        if divergences:
            finding = divergences[0]
            record["finding"] = finding
            verdict.outcome = Outcome.KILLED
            verdict.killers = (f"fuzz:{target.name}:{finding['digest']}",)
            verdict.note = (
                "a stable minimized input distinguished the mutant's semantic features "
                "or exception"
            )
        other_findings = [finding for finding in payload["findings"] if finding not in divergences]
        if other_findings:
            record["probe_errors"] = other_findings
        evidence.append(record)
        verdict.fuzz_evidence = (*verdict.fuzz_evidence, record)
    report.differential_fuzz = {
        "master_seed": master_seed,
        "case_budget": config.max_cases,
        "cases_executed": config.max_cases - remaining_cases,
        "seconds_budget": config.max_seconds,
        "seconds": round(time.monotonic() - started, 3),
        "generate": config.generate,
        "probes": probes,
        "failures": sum(
            any(
                key in item
                for key in (
                    "worker_failure",
                    "error",
                    "timeout",
                    "probe_errors",
                    "crash_artifact_error",
                )
            )
            for item in evidence
        ),
        "evidence": evidence,
        "stopped": (
            "case-limit"
            if remaining_cases <= 0
            else "time-limit"
            if time.monotonic() - started >= config.max_seconds
            else "complete"
        ),
    }
