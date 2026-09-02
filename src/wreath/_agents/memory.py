from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol, runtime_checkable

MemoryTrust = Literal["system", "user", "tool", "external"]


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    tenant: str
    principal_id: str
    conversation: str
    content: str
    trust: MemoryTrust
    provenance: str
    created_at: float

    def __post_init__(self) -> None:
        if not all(
            (
                self.memory_id,
                self.tenant,
                self.principal_id,
                self.conversation,
                self.content,
                self.provenance,
            )
        ):
            raise ValueError("memory records require IDs, ownership, content, and provenance")
        if self.trust not in {"system", "user", "tool", "external"}:
            raise ValueError(f"unsupported memory trust label {self.trust!r}")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not isfinite(self.created_at)
        ):
            raise ValueError("memory created_at must be finite")


@runtime_checkable
class MemoryStore(Protocol):
    retention_seconds: float

    async def append(self, record: MemoryRecord) -> None: ...

    async def recent(
        self, *, tenant: str, principal_id: str, conversation: str, limit: int
    ) -> Sequence[MemoryRecord]: ...

    async def erase(self, *, tenant: str, principal_id: str, conversation: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AssembledContext:
    records: tuple[MemoryRecord, ...]
    characters: int


class ContextAssembler:
    __slots__ = ("_max_chars", "_max_items", "_store")

    def __init__(self, store: MemoryStore, *, max_items: int = 32, max_chars: int = 16_000) -> None:
        if type(max_items) is not int or type(max_chars) is not int:
            raise TypeError("memory context limits must be integers")
        if max_items < 1 or max_chars < 1:
            raise ValueError("memory context limits must be positive")
        retention = getattr(store, "retention_seconds", None)
        if (
            isinstance(retention, bool)
            or not isinstance(retention, (int, float))
            or not isfinite(retention)
            or retention <= 0
        ):
            raise ValueError("memory store requires bounded positive retention_seconds")
        if not callable(getattr(store, "erase", None)):
            raise ValueError("memory store requires erase(tenant=, principal_id=, conversation=)")
        self._store = store
        self._max_items = max_items
        self._max_chars = max_chars

    @property
    def retention_seconds(self) -> float:
        return float(self._store.retention_seconds)

    async def assemble(
        self, *, tenant: str, principal_id: str, conversation: str
    ) -> AssembledContext:
        self._check_scope(tenant, principal_id, conversation)
        records = await self._store.recent(
            tenant=tenant,
            principal_id=principal_id,
            conversation=conversation,
            limit=self._max_items,
        )
        if len(records) > self._max_items:
            raise ValueError("memory store returned more than the requested bounded limit")
        ordered = sorted(records, key=lambda item: (-item.created_at, item.memory_id))
        selected: list[MemoryRecord] = []
        characters = 0
        for item in ordered:
            if item.tenant != tenant:
                raise ValueError(f"memory {item.memory_id!r} belongs to another tenant")
            if item.principal_id != principal_id:
                raise ValueError(f"memory {item.memory_id!r} belongs to another principal")
            if item.conversation != conversation:
                raise ValueError(f"memory {item.memory_id!r} belongs to another conversation")
            size = len(item.content)
            if characters + size > self._max_chars:
                continue
            selected.append(item)
            characters += size
        return AssembledContext(tuple(selected), characters)

    async def erase(self, *, tenant: str, principal_id: str, conversation: str) -> None:
        self._check_scope(tenant, principal_id, conversation)
        await self._store.erase(
            tenant=tenant,
            principal_id=principal_id,
            conversation=conversation,
        )

    async def remember(self, record: MemoryRecord) -> None:
        await self._store.append(record)

    @staticmethod
    def _check_scope(tenant: str, principal_id: str, conversation: str) -> None:
        if not all(
            isinstance(value, str) and value for value in (tenant, principal_id, conversation)
        ):
            raise ValueError("memory tenant, principal_id, and conversation must be non-empty")


__all__ = [
    "AssembledContext",
    "ContextAssembler",
    "MemoryRecord",
    "MemoryStore",
    "MemoryTrust",
]
