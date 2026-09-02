from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from wreath._agents.memory import ContextAssembler, MemoryRecord, MemoryTrust


@dataclass
class Store:
    records: list[MemoryRecord]
    retention_seconds: float = 3600.0

    def __post_init__(self) -> None:
        self.reads: list[tuple[str, str, str, int]] = []
        self.erased: list[tuple[str, str, str]] = []
        self.appended: list[MemoryRecord] = []

    async def append(self, record: MemoryRecord) -> None:
        self.appended.append(record)

    async def recent(
        self, *, tenant: str, principal_id: str, conversation: str, limit: int
    ) -> list[MemoryRecord]:
        self.reads.append((tenant, principal_id, conversation, limit))
        return self.records[:limit]

    async def erase(self, *, tenant: str, principal_id: str, conversation: str) -> None:
        self.erased.append((tenant, principal_id, conversation))


def record(
    memory_id: str,
    content: str,
    *,
    created_at: float,
    trust: MemoryTrust = "user",
    tenant: str = "tenant-a",
    principal_id: str = "user-7",
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tenant=tenant,
        principal_id=principal_id,
        conversation="conversation-2",
        content=content,
        trust=trust,
        provenance=f"chat:{memory_id}",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_context_assembly_is_one_bounded_read_and_deterministic() -> None:
    store = Store(
        [
            record("later-b", "bbbb", created_at=3.0),
            record("older", "old", created_at=1.0),
            record("later-a", "aaaa", created_at=3.0, trust="tool"),
        ]
    )
    assembler = ContextAssembler(store, max_items=3, max_chars=8)

    context = await assembler.assemble(
        tenant="tenant-a", principal_id="user-7", conversation="conversation-2"
    )

    assert store.reads == [("tenant-a", "user-7", "conversation-2", 3)]
    assert [(item.memory_id, item.content) for item in context.records] == [
        ("later-a", "aaaa"),
        ("later-b", "bbbb"),
    ]
    assert context.characters == 8
    assert context.records[0].trust == "tool"
    assert context.records[0].provenance == "chat:later-a"


@pytest.mark.asyncio
async def test_context_fails_closed_on_cross_tenant_or_principal_rows() -> None:
    for foreign in (
        record("foreign", "secret", created_at=1.0, tenant="tenant-b"),
        record("foreign", "secret", created_at=1.0, principal_id="user-8"),
        MemoryRecord(
            "foreign",
            "tenant-a",
            "user-7",
            "another-conversation",
            "secret",
            "user",
            "chat:foreign",
            1.0,
        ),
    ):
        assembler = ContextAssembler(Store([foreign]), max_items=3, max_chars=100)
        with pytest.raises(ValueError, match="tenant|principal|conversation"):
            await assembler.assemble(
                tenant="tenant-a", principal_id="user-7", conversation="conversation-2"
            )


@pytest.mark.asyncio
async def test_memory_retention_and_erasure_are_explicit() -> None:
    store = Store([])
    assembler = ContextAssembler(store, max_items=3, max_chars=100)
    remembered = record("remembered", "fact", created_at=1.0, trust="external")

    await assembler.remember(remembered)
    await assembler.erase(tenant="tenant-a", principal_id="user-7", conversation="conversation-2")
    assert assembler.retention_seconds == 3600.0
    assert store.appended == [remembered]
    assert store.erased == [("tenant-a", "user-7", "conversation-2")]

    store.retention_seconds = 0
    with pytest.raises(ValueError, match="bounded positive retention"):
        ContextAssembler(store, max_items=3, max_chars=100)


@pytest.mark.parametrize(
    ("max_items", "max_chars"), [(0, 10), (2, 0), (True, 10), (2, 1.5)]
)
def test_context_limits_must_be_positive(max_items: Any, max_chars: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="limits"):
        ContextAssembler(Store([]), max_items=max_items, max_chars=max_chars)


@pytest.mark.asyncio
async def test_context_refuses_a_store_that_violates_the_requested_bound() -> None:
    class UnboundedStore(Store):
        async def recent(
            self, *, tenant: str, principal_id: str, conversation: str, limit: int
        ) -> list[MemoryRecord]:
            return self.records

    records = [record(str(index), "x", created_at=float(index)) for index in range(3)]
    assembler = ContextAssembler(UnboundedStore(records), max_items=2, max_chars=100)

    with pytest.raises(ValueError, match="bounded limit"):
        await assembler.assemble(
            tenant="tenant-a", principal_id="user-7", conversation="conversation-2"
        )


def test_memory_records_refuse_missing_facts_and_unknown_trust() -> None:
    with pytest.raises(ValueError, match="require IDs"):
        record("", "content", created_at=1.0)
    with pytest.raises(ValueError, match="trust label"):
        record("memory", "content", created_at=1.0, trust=cast(MemoryTrust, "model"))


def test_memory_store_contract_refuses_non_numeric_retention_and_missing_erasure() -> None:
    store = Store([])
    store.retention_seconds = cast(float, "forever")
    with pytest.raises(ValueError, match="bounded positive retention"):
        ContextAssembler(store)

    store.retention_seconds = 60.0
    store.erase = cast(Any, None)
    with pytest.raises(ValueError, match="requires erase"):
        ContextAssembler(store)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_memory_times_must_be_finite(value: float) -> None:
    with pytest.raises(ValueError, match="created_at.*finite"):
        record("memory", "content", created_at=value)

    with pytest.raises(ValueError, match="bounded positive retention"):
        ContextAssembler(Store([], retention_seconds=value))


@pytest.mark.asyncio
async def test_empty_memory_scope_refuses_before_store_access() -> None:
    store = Store([])
    assembler = ContextAssembler(store)

    for values in (
        ("", "user", "conversation"),
        ("tenant", "", "conversation"),
        ("tenant", "user", ""),
    ):
        with pytest.raises(ValueError, match="non-empty"):
            await assembler.assemble(
                tenant=values[0],
                principal_id=values[1],
                conversation=values[2],
            )

    assert store.reads == []
