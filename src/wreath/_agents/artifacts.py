from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from ..objects import ObjectStat, ObjectStore
from ..provenance import Provenance
from .identity import principal_partition

ArtifactTrust = Literal["system", "user", "model", "tool", "external"]
_MAX_KEY_PART_BYTES = 1024


class ArtifactLimitExceeded(ValueError):
    pass


def _digest_parts(*parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"wreath.agent.artifact.v1")
    for part in parts:
        if not isinstance(part, str) or not part:
            raise ValueError("artifact key parts must be non-empty strings")
        if len(part) > _MAX_KEY_PART_BYTES:
            raise ValueError("artifact key parts must contain at most 1024 UTF-8 bytes")
        try:
            encoded = part.encode()
        except UnicodeEncodeError:
            raise ValueError("artifact key parts must be valid UTF-8") from None
        if len(encoded) > _MAX_KEY_PART_BYTES:
            raise ValueError("artifact key parts must contain at most 1024 UTF-8 bytes")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    key: str
    artifact_id: str
    ordinal: int
    tenant: str
    principal_id: str
    conversation: str
    digest: str
    media_type: str
    trust: ArtifactTrust
    provenance: Provenance
    stat: ObjectStat


class AgentArtifactManager:
    __slots__ = ("_max_artifacts", "_max_bytes", "_store")

    def __init__(self, store: ObjectStore, *, max_bytes: int, max_artifacts: int) -> None:
        if type(max_bytes) is not int:
            raise TypeError("agent artifact max_bytes must be an integer")
        if type(max_artifacts) is not int:
            raise TypeError("agent artifact max_artifacts must be an integer")
        if max_bytes < 1:
            raise ValueError("agent artifact max_bytes must be positive")
        if max_artifacts < 1:
            raise ValueError("agent artifact max_artifacts must be positive")
        self._store = store
        self._max_bytes = max_bytes
        self._max_artifacts = max_artifacts

    def _scope(self, context: Any) -> tuple[str, str, str, str]:
        tenant = context.tenant
        conversation = context.conversation
        resolved_principal, partition_id = principal_partition(
            context.principal, label="artifact"
        )
        if not isinstance(tenant, str) or not tenant:
            raise ValueError("artifact tenant must be a non-empty string")
        if not isinstance(conversation, str) or not conversation:
            raise ValueError("artifact conversation must be a non-empty string")
        return tenant, resolved_principal, partition_id, conversation

    def _check_ordinal(self, ordinal: int) -> None:
        if type(ordinal) is not int:
            raise TypeError("artifact ordinal must be an integer")
        if ordinal < 0 or ordinal >= self._max_artifacts:
            raise ArtifactLimitExceeded(
                f"artifact ordinal {ordinal} exceeds count ceiling {self._max_artifacts}"
            )

    @staticmethod
    def _check_metadata(media_type: str, trust: str) -> None:
        if not isinstance(media_type, str) or not media_type:
            raise ValueError("artifact media_type must be a non-empty string")
        if len(media_type) > 1024 or any(
            ord(character) < 32 or ord(character) == 127 for character in media_type
        ):
            raise ValueError(
                "artifact media_type must contain at most 1024 characters and no controls"
            )
        if trust not in {"system", "user", "model", "tool", "external"}:
            raise ValueError(f"unsupported artifact trust label {trust!r}")

    def _resolved_key(
        self, context: Any, artifact_id: str, *, ordinal: int
    ) -> tuple[str, tuple[str, str, str]]:
        self._check_ordinal(ordinal)
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("artifact_id must be a non-empty string")
        tenant, resolved_principal, partition_id, conversation = self._scope(context)
        identity = _digest_parts(tenant, partition_id, conversation, artifact_id, str(ordinal))
        key = f"agents/artifacts/{identity[:2]}/{identity}"
        return key, (tenant, resolved_principal, conversation)

    def key(self, context: Any, artifact_id: str, *, ordinal: int) -> str:
        return self._resolved_key(context, artifact_id, ordinal=ordinal)[0]

    def _artifact(
        self,
        scope: tuple[str, str, str],
        *,
        artifact_id: str,
        ordinal: int,
        digest: bytes,
        media_type: str,
        trust: ArtifactTrust,
        stat: ObjectStat,
    ) -> AgentArtifact:
        tenant, principal_id, conversation = scope
        provenance = Provenance(
            digest,
            name=artifact_id,
            media_type=media_type,
        )
        return AgentArtifact(
            stat.key,
            artifact_id,
            ordinal,
            tenant,
            principal_id,
            conversation,
            digest.hex(),
            media_type,
            trust,
            provenance,
            stat,
        )

    async def write(
        self,
        context: Any,
        *,
        artifact_id: str,
        ordinal: int,
        body: bytes | bytearray | memoryview,
        media_type: str = "application/octet-stream",
        trust: ArtifactTrust = "model",
    ) -> AgentArtifact:
        self._check_metadata(media_type, trust)
        key, scope = self._resolved_key(context, artifact_id, ordinal=ordinal)
        if not isinstance(body, bytes | bytearray | memoryview):
            raise TypeError("artifact body must be bytes, bytearray, or memoryview")
        body_size = body.nbytes if isinstance(body, memoryview) else len(body)
        if body_size > self._max_bytes:
            raise ArtifactLimitExceeded(
                f"artifact {artifact_id!r} exceeds byte ceiling {self._max_bytes}"
            )
        stable_body = body if isinstance(body, bytes) else bytes(body)
        digest = hashlib.sha256(stable_body).digest()
        stat = await self._store.write(key, stable_body, content_type=media_type)
        return self._artifact(
            scope,
            artifact_id=artifact_id,
            ordinal=ordinal,
            digest=digest,
            media_type=media_type,
            trust=trust,
            stat=stat,
        )

    async def write_stream(
        self,
        context: Any,
        *,
        artifact_id: str,
        ordinal: int,
        chunks: AsyncIterable[bytes],
        media_type: str = "application/octet-stream",
        trust: ArtifactTrust = "model",
    ) -> AgentArtifact:
        self._check_metadata(media_type, trust)
        key, scope = self._resolved_key(context, artifact_id, ordinal=ordinal)
        digest = hashlib.sha256()
        total = 0

        async def bounded() -> AsyncIterator[bytes]:
            nonlocal total
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("artifact stream chunks must be bytes")
                next_total = total + len(chunk)
                if next_total > self._max_bytes:
                    raise ArtifactLimitExceeded(
                        f"artifact {artifact_id!r} exceeds byte ceiling {self._max_bytes}"
                    )
                total = next_total
                digest.update(chunk)
                yield chunk

        stat = await self._store.write_stream(key, bounded(), content_type=media_type)
        return self._artifact(
            scope,
            artifact_id=artifact_id,
            ordinal=ordinal,
            digest=digest.digest(),
            media_type=media_type,
            trust=trust,
            stat=stat,
        )

    async def erase(self, context: Any, artifact_id: str, *, ordinal: int) -> None:
        await self._store.delete(self.key(context, artifact_id, ordinal=ordinal))


__all__ = [
    "AgentArtifact",
    "AgentArtifactManager",
    "ArtifactLimitExceeded",
    "ArtifactTrust",
]
