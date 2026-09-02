from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from wreath.jobs import JobContext

SERVICE_URL = "https://smba.trafficmanager.net/amer/"
APP_ID = "00000000-0000-4000-8000-000000000001"
ENTRA_TENANT = "11111111-1111-4111-8111-111111111111"
AAD_OBJECT_ID = "22222222-2222-4222-8222-222222222222"


def activity(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "message",
        "id": "activity-1",
        "timestamp": "2026-09-02T04:00:00Z",
        "serviceUrl": SERVICE_URL,
        "channelId": "msteams",
        "from": {
            "id": "29:opaque-teams-member-id",
            "aadObjectId": AAD_OBJECT_ID,
            "name": "Ada Lovelace",
        },
        "recipient": {"id": APP_ID, "name": "Wreath"},
        "conversation": {
            "id": "19:conversation@thread.tacv2",
            "conversationType": "channel",
            "tenantId": ENTRA_TENANT,
        },
        "text": "run deploy",
        "locale": "en-AU",
        "channelData": {
            "tenant": {"id": ENTRA_TENANT},
            "team": {"id": "19:team@thread.tacv2"},
            "channel": {"id": "19:channel@thread.tacv2"},
        },
    }
    result = deepcopy(value)
    for key, replacement in changes.items():
        if replacement is None:
            result.pop(key, None)
        else:
            result[key] = replacement
    return result


@dataclass(slots=True)
class RecordingFetch:
    responses: dict[str, Any]
    calls: list[str] = field(default_factory=list)

    async def __call__(self, url: str) -> Any:
        self.calls.append(url)
        return deepcopy(self.responses[url])


@dataclass(slots=True)
class RecordingConnector:
    responses: list[Any] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)

    async def send(self, request: Any) -> Any:
        self.requests.append(request)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {"id": f"sent-{len(self.requests)}"}


@dataclass(slots=True)
class RecordingAudit:
    records: list[Any] = field(default_factory=list)

    async def append(self, record: Any) -> None:
        self.records.append(record)


@dataclass(slots=True)
class AcceptingVerifier:
    calls: list[tuple[str | None, dict[str, Any]]] = field(default_factory=list)
    startups: int = 0

    async def startup(self) -> int:
        self.startups += 1
        return 1

    def verify(self, authorization: str | None, payload: dict[str, Any]) -> None:
        self.calls.append((authorization, payload))


@dataclass(slots=True)
class MemoryInbox:
    claims: set[tuple[str, str, str]] = field(default_factory=set)
    atomic_calls: list[dict[str, Any]] = field(default_factory=list)

    async def claim(self, *, provider: str, installation: str, delivery: str) -> bool:
        key = (provider, installation, delivery)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def claim_and_enqueue(
        self,
        *,
        source: str,
        envelope: Any,
        enqueue: Any,
        **_options: Any,
    ) -> bool:
        provider, installation = source.split(":", 1)
        key = (provider, installation, envelope.id)
        if key in self.claims:
            return False
        self.atomic_calls.append({"source": source, "envelope": envelope})
        await enqueue(transaction=self)
        self.claims.add(key)
        return True


@dataclass(slots=True)
class RecordingJobs:
    handlers: dict[str, Any] = field(default_factory=dict)
    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    attempts: list[tuple[str, int]] = field(default_factory=list)

    def task(self, name: str, **options: Any) -> Any:
        def register(handler: Any) -> Any:
            self.handlers[name] = (handler, options)
            return handler

        return register

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        tenant: str = "",
        run_at: Any = None,
        tx: Any = None,
    ) -> None:
        self.pending.append(
            (
                name,
                payload,
                {"key": key, "tenant": tenant, "run_at": run_at, "fence": 1, "tx": tx},
            )
        )

    async def run_next(self) -> Any:
        name, payload, options = self.pending.pop(0)
        handler, _ = self.handlers[name]
        attempt = 1
        self.attempts.append((name, attempt))
        job = JobContext(
            job_id=1,
            task=name,
            attempt=attempt,
            fence=options["fence"],
            key=options["key"],
            tenant=options["tenant"],
        )
        return await handler(job, payload)


@dataclass(slots=True)
class MemoryInstallations:
    rows: dict[tuple[str, str], Any] = field(default_factory=dict)
    puts: int = 0
    deletes: int = 0

    async def put(self, key: tuple[str, str], value: Any) -> None:
        self.puts += 1
        self.rows[key] = value

    async def get(self, key: tuple[str, str]) -> Any:
        return self.rows.get(key)

    async def delete(self, key: tuple[str, str]) -> None:
        self.deletes += 1
        self.rows.pop(key, None)
