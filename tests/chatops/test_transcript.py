from __future__ import annotations

from wreath import Wreath
from wreath.chat import AgentEvent, ChatContext, ChatOps
from wreath.testing import ChatTranscript, TestClient


async def test_provider_neutral_transcript_drives_a_typed_command() -> None:
    app = Wreath()
    chat = ChatOps(app, name="ops")

    @chat.command("scale")
    async def scale(replicas: int, context: ChatContext) -> str:
        return f"{context.provider}:{replicas}"

    transcript = TestClient(app).chat(
        "ops",
        provider="slack",
        installation="T1",
        tenant="slack:T1",
        actor="U1",
        conversation="C1",
    )
    turn = await transcript.command("scale", replicas="3")

    assert isinstance(transcript, ChatTranscript)
    assert turn.reply is not None
    assert turn.reply.content == "slack:3"
    assert transcript.turns == (turn,)


async def test_transcript_finds_an_application_defined_chatops_subclass() -> None:
    class DeploymentChatOps(ChatOps):
        pass

    app = Wreath()
    chat = DeploymentChatOps(app, name="ops")

    @chat.command("ping")
    async def ping() -> str:
        return "pong"

    transcript = TestClient(app).chat(
        "ops",
        provider="slack",
        installation="T1",
        tenant="slack:T1",
        actor="U1",
        conversation="C1",
    )

    turn = await transcript.command("ping")

    assert turn.reply is not None and turn.reply.content == "pong"


async def test_provider_neutral_transcript_collects_durable_agent_events() -> None:
    app = Wreath()
    chat = ChatOps(app, name="ops")

    @chat.command("ask", execution="durable")
    async def ask(context: ChatContext, prompt: str) -> str:
        await context.emit(AgentEvent.text(prompt.upper()))
        return "complete"

    transcript = TestClient(app).chat(
        "ops",
        provider="teams",
        installation="tenant-1",
        tenant="teams:tenant-1",
        actor="user-1",
        conversation="conversation-1",
    )
    turn = await transcript.command("ask", prompt="hello")

    assert turn.events == (AgentEvent.text("HELLO"),)
    assert turn.reply is not None and turn.reply.content == "complete"
