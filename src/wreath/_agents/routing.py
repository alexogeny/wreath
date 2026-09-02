from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, ClassVar, cast

from .core import BackplaneError, ModelRequest, ModelResponseEvent, ModelTarget


def _facts(value: Iterable[str], *, label: str) -> frozenset[str]:
    if isinstance(value, str):
        raise ValueError(f"{label} must contain non-empty strings")
    facts = frozenset(value)
    if not facts or any(not isinstance(fact, str) or not fact for fact in facts):
        raise ValueError(f"{label} must contain non-empty strings")
    return facts


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    name: str
    target: ModelTarget
    capabilities: frozenset[str]
    regions: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("model candidate name must be a non-empty string")
        if not isinstance(self.target, ModelTarget):
            raise TypeError("model candidate target must be a ModelTarget")
        object.__setattr__(
            self,
            "capabilities",
            _facts(self.capabilities, label=f"model candidate {self.name!r} capabilities"),
        )
        object.__setattr__(
            self,
            "regions",
            _facts(self.regions, label=f"model candidate {self.name!r} regions"),
        )


@dataclass(frozen=True, slots=True)
class ModelRoutePolicy:
    tenant: str
    candidates: tuple[str, ...]
    required_capabilities: frozenset[str]
    allowed_regions: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, str) or not self.tenant:
            raise ValueError("model route policy tenant must be a non-empty string")
        names = tuple(self.candidates)
        if not names:
            raise ValueError(f"model route policy for tenant {self.tenant!r} needs candidates")
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("model route policy candidate names must be non-empty strings")
        if len(set(names)) != len(names):
            raise ValueError(
                f"model route policy for tenant {self.tenant!r} has a duplicate candidate"
            )
        object.__setattr__(self, "candidates", names)
        object.__setattr__(
            self,
            "required_capabilities",
            _facts(
                self.required_capabilities,
                label=f"model route policy for tenant {self.tenant!r} capabilities",
            ),
        )
        object.__setattr__(
            self,
            "allowed_regions",
            _facts(
                self.allowed_regions,
                label=f"model route policy for tenant {self.tenant!r} regions",
            ),
        )


@dataclass(frozen=True, slots=True)
class _CompiledPolicy:
    required_capabilities: frozenset[str]
    allowed_regions: frozenset[str]
    candidates: tuple[ModelCandidate, ...]


def _healthy(_candidate: ModelCandidate) -> bool:
    return True


def _runtime_facts(value: Any, *, label: str) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise BackplaneError(f"request metadata {label!r} must contain strings")
    facts = frozenset(value)
    if any(not isinstance(fact, str) or not fact for fact in facts):
        raise BackplaneError(f"request metadata {label!r} must contain non-empty strings")
    return cast(frozenset[str], facts)


class RoutedBackplane:
    name: ClassVar[str] = "routed"

    __slots__ = ("_health", "_policies", "candidates", "tenants")

    def __init__(
        self,
        candidates: Iterable[ModelCandidate],
        policies: Iterable[ModelRoutePolicy],
        *,
        health: Callable[[ModelCandidate], bool] | None = None,
    ) -> None:
        compiled_candidates: dict[str, ModelCandidate] = {}
        for candidate in tuple(candidates):
            if not isinstance(candidate, ModelCandidate):
                raise TypeError("RoutedBackplane candidates must be ModelCandidate values")
            if candidate.name in compiled_candidates:
                raise ValueError(f"duplicate model candidate {candidate.name!r}")
            compiled_candidates[candidate.name] = candidate
        if not compiled_candidates:
            raise ValueError("RoutedBackplane needs at least one model candidate")
        if health is not None and not callable(health):
            raise TypeError("RoutedBackplane health must be callable")
        compiled_policies: dict[str, _CompiledPolicy] = {}
        for policy in tuple(policies):
            if not isinstance(policy, ModelRoutePolicy):
                raise TypeError("RoutedBackplane policies must be ModelRoutePolicy values")
            if policy.tenant in compiled_policies:
                raise ValueError(f"duplicate model route policy for tenant {policy.tenant!r}")
            selected: list[ModelCandidate] = []
            for name in policy.candidates:
                candidate = compiled_candidates.get(name)
                if candidate is None:
                    raise ValueError(
                        f"model route policy for tenant {policy.tenant!r} selects "
                        f"unknown candidate {name!r}"
                    )
                if policy.required_capabilities.issubset(candidate.capabilities) and not (
                    policy.allowed_regions.isdisjoint(candidate.regions)
                ):
                    selected.append(candidate)
            if not selected:
                raise ValueError(
                    f"model route policy for tenant {policy.tenant!r} has no statically "
                    "eligible candidate"
                )
            compiled_policies[policy.tenant] = _CompiledPolicy(
                policy.required_capabilities,
                policy.allowed_regions,
                tuple(selected),
            )
        if not compiled_policies:
            raise ValueError("RoutedBackplane needs at least one tenant policy")
        self._health = _healthy if health is None else health
        self._policies: Mapping[str, _CompiledPolicy] = MappingProxyType(compiled_policies)
        self.candidates = tuple(compiled_candidates)
        self.tenants = frozenset(compiled_policies)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]:
        metadata = request.metadata
        tenant = metadata.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise BackplaneError("model routing requires request.metadata['tenant']")
        policy = self._policies.get(tenant)
        if policy is None:
            raise BackplaneError(f"no model route policy for tenant {tenant!r}")
        requested_capabilities = metadata.get("required_capabilities")
        required = policy.required_capabilities
        if requested_capabilities is not None:
            required = required | _runtime_facts(
                requested_capabilities,
                label="required_capabilities",
            )
        requested_regions = metadata.get("allowed_regions")
        allowed = policy.allowed_regions
        if requested_regions is not None:
            allowed = allowed & _runtime_facts(requested_regions, label="allowed_regions")
        request_eligible = False
        last_error: BackplaneError | None = None
        for candidate in policy.candidates:
            if not required.issubset(candidate.capabilities) or allowed.isdisjoint(
                candidate.regions
            ):
                continue
            request_eligible = True
            if not self._health(candidate):
                continue
            routed = replace(request, model=candidate.target.model)
            emitted = False
            try:
                async for event in candidate.target.backplane.stream(routed):
                    emitted = True
                    yield event
                if emitted:
                    return
                last_error = BackplaneError(
                    f"model candidate {candidate.name!r} returned no events",
                    retryable=True,
                )
            except BackplaneError as error:
                if emitted or error.output_started or not error.retryable:
                    raise
                last_error = error
        if last_error is not None:
            raise last_error
        if request_eligible:
            raise BackplaneError(
                f"no healthy model candidate for tenant {tenant!r}",
                retryable=True,
            )
        raise BackplaneError(f"no eligible model candidate for tenant {tenant!r}")


__all__ = ["ModelCandidate", "ModelRoutePolicy", "RoutedBackplane"]
