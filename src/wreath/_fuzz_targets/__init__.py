from __future__ import annotations

from dataclasses import dataclass

from wreath._fuzz import FuzzTarget

from .graphql import TARGET as GRAPHQL
from .h2 import TARGET as H2
from .http1 import TARGET as HTTP1
from .http_replay import TARGET as HTTP_REPLAY
from .multipart import TARGET as MULTIPART
from .xml import TARGET as XML

TARGETS = (GRAPHQL, H2, HTTP_REPLAY, HTTP1, MULTIPART, XML)
_BY_NAME = {target.name: target for target in TARGETS}


@dataclass(frozen=True, slots=True)
class TargetInventoryEntry:
    name: str
    boundary: str
    oracle: str
    owner: str
    native_harness: str | None = None


INVENTORY = (
    TargetInventoryEntry(
        "graphql-parser",
        "GraphQL UTF-8 documents entering the native parser",
        "repeat parse equality plus declared parser limits",
        "graphql",
        native_harness="graphql",
    ),
    TargetInventoryEntry(
        "h2-frames",
        "HTTP/2 frame bytes emitted by native transports",
        "byte-exact reconstruction of every consumed frame",
        "server",
        native_harness="h2",
    ),
    TargetInventoryEntry(
        "http-replay-codec",
        "persisted outbound HTTP exchange records",
        "decode-encode-decode object equality",
        "recording",
        native_harness="http-replay",
    ),
    TargetInventoryEntry(
        "http1-parser",
        "untrusted HTTP/1 request bytes and fragmentation",
        "fragmentation invariance plus restricted independent grammar",
        "server",
        native_harness="http1",
    ),
    TargetInventoryEntry(
        "multipart-parser",
        "multipart/form-data request bodies",
        "bounded materialization and normalized output invariants",
        "binding",
        native_harness="multipart",
    ),
    TargetInventoryEntry(
        "xml-parser",
        "untrusted XML document bytes",
        "canonicalization idempotence and source-span invariants",
        "xml",
        native_harness="xml",
    ),
)

if {entry.name for entry in INVENTORY} != set(_BY_NAME):
    raise RuntimeError("fuzz target inventory must name every registered target exactly once")


def by_name(name: str) -> FuzzTarget:
    try:
        return _BY_NAME[name]
    except KeyError:
        choices = ", ".join(_BY_NAME)
        raise ValueError(f"unknown fuzz target {name!r}; choose one of: {choices}") from None


def for_mutation(source_file: str, operator_name: str) -> tuple[FuzzTarget, ...]:
    return tuple(
        target
        for target in TARGETS
        if source_file in target.source_files and operator_name in target.operator_names
    )


__all__ = ["INVENTORY", "TARGETS", "TargetInventoryEntry", "by_name", "for_mutation"]
