from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath import Wreath
from wreath.chat import AgentEvent, AgentRequest, ChatContext, ChatCorrelation, ChatOps
from wreath.chat.slack import Slack
from wreath.testing import TestClient

from .conftest import NOW, SIGNING_SECRET, RecordingTransport, form_body, signed_headers
from .test_responses_and_delivery import MemoryInbox, RecordingJobs


@dataclass
class ReportingJobContext:
    job_id: int = 7
    fence: int = 11
    tenant: str = "slack:T123"
    key: str | None = None
    trace_context: str = "00-trace-parent"
    reports: list[tuple[float, str]] = field(default_factory=list)

    def report(self, percent: float, message: str = "") -> None:
        self.reports.append((percent, message))


def job_for(payload: dict[str, Any], *, fence: int = 11) -> ReportingJobContext:
    context = payload["context"]
    return ReportingJobContext(
        fence=fence,
        tenant=context["tenant"],
        key=context["delivery_id"],
    )


async def enqueue_agent(
    *, jobs: RecordingJobs, transport: RecordingTransport, handler: Any
) -> tuple[str, dict[str, Any]]:
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(
            Slack(
                signing_secret=SIGNING_SECRET,
                clock=lambda: NOW,
                http_client=transport,
            ),
        ),
        jobs=jobs,
        inbox=MemoryInbox(),
    )
    chat.command("agent", execution="durable")(handler)
    values = {
        "api_app_id": "A123",
        "team_id": "T123",
        "channel_id": "C123",
        "user_id": "U123",
        "command": "/agent",
        "text": '--prompt "ship production"',
        "response_url": "https://hooks.slack.com/commands/T123/1/secret",
    }
    body = form_body(**values)
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    response = await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)
    assert (response.status, response.body) == (200, b"")
    task, payload, _key = jobs.enqueued[0]
    return task, payload


async def test_registered_durable_handler_activates_agent_context_and_replaces_original() -> None:
    jobs = RecordingJobs()
    transport = RecordingTransport()
    seen: list[ChatContext] = []

    async def agent(context: ChatContext, prompt: str) -> None:
        seen.append(context)
        await context.emit(AgentEvent.progress("reading", percent=10))
        await context.emit(AgentEvent.text(f"done: {prompt}"))
        await context.emit(AgentEvent.completed())

    task, payload = await enqueue_agent(jobs=jobs, transport=transport, handler=agent)
    job = job_for(payload)
    await jobs.registered[task](job, payload)

    context = seen[0]
    assert context.job_context is job
    assert context.job_context.fence == 11
    assert context.agent_request == AgentRequest(
        tenant="slack:T123",
        actor="U123",
        conversation="C123",
        prompt="ship production",
        correlation=ChatCorrelation(
            interaction_id=context.delivery_id,
            job_id="7",
            trace_id="00-trace-parent",
        ),
        native=context.native,
    )
    assert job.reports == [(10.0, "reading")]
    assert len(transport.requests) == 1
    assert transport.requests[0][0:2] == (
        "POST",
        "https://hooks.slack.com/commands/T123/1/secret",
    )
    assert json.loads(transport.requests[0][3]) == {
        "replace_original": True,
        "text": "done: ship production",
    }


async def test_durable_retry_repeats_only_the_same_original_message_replacement() -> None:
    jobs = RecordingJobs()
    transport = RecordingTransport()
    attempts = 0

    async def agent(context: ChatContext, prompt: str) -> None:
        nonlocal attempts
        attempts += 1
        await context.emit(AgentEvent.text(f"done: {prompt}"))
        await context.emit(AgentEvent.completed())
        if attempts == 1:
            raise RuntimeError("retry after delivery")

    task, payload = await enqueue_agent(jobs=jobs, transport=transport, handler=agent)

    with pytest.raises(RuntimeError, match="retry after delivery"):
        await jobs.registered[task](job_for(payload, fence=11), payload)
    await jobs.registered[task](job_for(payload, fence=12), payload)

    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert json.loads(transport.requests[0][3])["replace_original"] is True


async def test_durable_worker_refuses_text_beyond_the_retained_character_bound() -> None:
    jobs = RecordingJobs()
    transport = RecordingTransport()
    emitters: list[Any] = []
    retained = "x" * 40_000

    async def agent(context: ChatContext, prompt: str) -> None:
        emitters.append(context.emit)
        await context.emit(AgentEvent.text(retained))
        await context.emit(AgentEvent.text("over the bound"))

    task, payload = await enqueue_agent(jobs=jobs, transport=transport, handler=agent)

    with pytest.raises(
        RuntimeError,
        match="Wreath's 40,000-character durable Slack bound",
    ):
        await jobs.registered[task](job_for(payload), payload)

    emitter = emitters[0]
    assert emitter._length == 40_000
    assert emitter._parts == [retained]
    assert transport.requests == []
