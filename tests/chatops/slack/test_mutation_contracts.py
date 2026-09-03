from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from wreath import Wreath
from wreath.chat import AgentEvent, ChatContext, ChatOps, IdentityLinkChallenge
from wreath.chat.slack import (
    Slack,
    SlackInstallation,
    SlackRateLimited,
    _command_arguments,
    _DurableEmitter,
    _external_identity_payload,
    _Inbound,
    _nested_text,
    _task_name,
    _Verified,
)
from wreath.http_client import ClientResponse
from wreath.testing import TestClient

from .conftest import (
    NOW,
    SIGNING_SECRET,
    RecordingTransport,
    form_body,
    json_body,
    signed_headers,
)
from .test_responses_and_delivery import MemoryInbox, RecordingJobs


def installation(**overrides: object) -> SlackInstallation:
    values = {
        "app_id": "A123",
        "team_id": "T123",
        "bot_token": "xoxb-secret",
        "bot_user_id": "UAPP",
        "scopes": frozenset({"chat:write"}),
    }
    values.update(overrides)
    return SlackInstallation(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"app_id": ""}, "app_id"),
        ({"bot_token": ""}, "bot_token"),
        ({"bot_user_id": ""}, "bot_user_id"),
        ({"team_id": None}, "workspace.*team_id"),
        (
            {"team_id": None, "enterprise_id": None, "is_enterprise_install": True},
            "enterprise_id",
        ),
        (
            {"team_id": "T123", "enterprise_id": "E123", "is_enterprise_install": True},
            "team_id=None",
        ),
    ],
)
def test_installation_refuses_each_incomplete_owner_fact(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        installation(**overrides)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_age": 0}, "max_age"),
        ({"max_retries": -1}, "max_retries"),
        ({"replay_entries": 0}, "replay_entries"),
    ],
)
def test_provider_refuses_each_non_positive_or_negative_bound(
    options: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Slack(signing_secret=SIGNING_SECRET, **options)


@pytest.mark.parametrize(
    "event_scopes",
    [{"": ("channels:read",)}, {"message": ()}, {"message": ("",)}],
)
def test_provider_refuses_incomplete_custom_event_scope_mapping(
    event_scopes: dict[str, tuple[str, ...]],
) -> None:
    with pytest.raises(ValueError, match="event_scopes"):
        Slack(signing_secret=SIGNING_SECRET, event_scopes=event_scopes)


def test_each_incomplete_event_scope_fact_is_independently_refused() -> None:
    for event_scopes in ({"": ("read",)}, {"event": ()}, {"event": ("",)}):
        with pytest.raises(ValueError, match="event_scopes"):
            Slack(signing_secret=SIGNING_SECRET, event_scopes=event_scopes)


async def test_durable_emitter_lifecycle_and_progress_without_percent() -> None:
    reports: list[tuple[float, str]] = []
    job = SimpleNamespace(report=lambda percent, message="": reports.append((percent, message)))
    emitter = _DurableEmitter(Slack(signing_secret=SIGNING_SECRET), job, None)

    await emitter(AgentEvent.progress("still working"))
    await emitter(AgentEvent.progress("", percent=10))
    await emitter(AgentEvent("progress", None, 20))
    await emitter(AgentEvent.text(""))
    await emitter(AgentEvent.completed())

    assert reports == [(10.0, ""), (20.0, "")]
    with pytest.raises(RuntimeError, match="after completed"):
        await emitter(AgentEvent.text("late"))


async def test_durable_emitter_refuses_unknown_events_and_output_without_response_url() -> None:
    emitter = _DurableEmitter(
        Slack(signing_secret=SIGNING_SECRET), SimpleNamespace(report=lambda *_: None), None
    )
    with pytest.raises(ValueError, match="unsupported AgentEvent kind 'future'"):
        await emitter(AgentEvent("future"))
    await emitter(AgentEvent.text("result"))
    with pytest.raises(RuntimeError, match="validated response_url"):
        await emitter(AgentEvent.completed())


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"type":"url_verification","challenge":7}',
        b'{"type":"future"}',
        b'{"type":"event_callback","team_id":"T123","event_id":"E1","event":[]}',
        b'{"type":"event_callback","team_id":"T123","event_id":"E1","event":{}}',
    ],
)
def test_event_parser_refuses_each_invalid_envelope_shape(body: bytes) -> None:
    slack = Slack(signing_secret=SIGNING_SECRET)
    with pytest.raises(ValueError):
        slack._parse_event(_Verified(body, int(NOW), {}))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"[]", "JSON object"),
        (b'{"type":"url_verification","challenge":7}', "challenge must be a string"),
        (b'{"type":"future"}', "event_callback or url_verification"),
        (
            b'{"type":"event_callback","team_id":"T123","event_id":"E1","event":[]}',
            "inner event type",
        ),
    ],
)
def test_event_parser_names_the_first_invalid_envelope_fact(body: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Slack(signing_secret=SIGNING_SECRET)._parse_event(_Verified(body, int(NOW), {}))


def test_event_parser_prefers_context_team_and_authoritative_enterprise_facts() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T-envelope",
            "context_team_id": "T-context",
            "context_enterprise_id": "E-context",
            "event_id": "Ev1",
            "authorizations": [
                None,
                {"enterprise_id": "E-other", "is_enterprise_install": "true"},
            ],
            "event": {"type": "app_mention"},
        }
    ).encode()
    inbound = Slack(signing_secret=SIGNING_SECRET)._parse_event(_Verified(body, int(NOW), {}))
    assert (inbound.installation, inbound.enterprise_id, inbound.is_enterprise_install) == (
        "T-context",
        "E-context",
        True,
    )


def test_event_authorization_facts_keep_the_first_owner_and_sticky_enterprise_flag() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev1",
            "authorizations": [
                {"enterprise_id": "E-first", "is_enterprise_install": True},
                {"enterprise_id": "E-second", "is_enterprise_install": False},
            ],
            "event": {"type": "app_mention"},
        }
    ).encode()
    inbound = Slack(signing_secret=SIGNING_SECRET)._parse_event(_Verified(body, int(NOW), {}))
    assert (inbound.enterprise_id, inbound.is_enterprise_install) == ("E-first", True)


def test_non_list_authorizations_are_ignored() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev1",
            "authorizations": {"enterprise_id": "attacker-shape"},
            "event": {"type": "app_mention"},
        }
    ).encode()
    inbound = Slack(signing_secret=SIGNING_SECRET)._parse_event(_Verified(body, int(NOW), {}))
    assert (inbound.enterprise_id, inbound.is_enterprise_install) == (None, False)


@pytest.mark.parametrize(
    "payload",
    ["[]", "{}", '{"actions":{}}', '{"actions":[]}', '{"actions":[null]}'],
)
def test_interaction_parser_refuses_each_invalid_action_shape(payload: str) -> None:
    body = urlencode({"payload": payload}).encode()
    slack = Slack(signing_secret=SIGNING_SECRET)
    with pytest.raises(ValueError):
        slack._parse_form(_Verified(body, int(NOW), {}))


def test_non_list_actions_are_refused_before_indexing() -> None:
    body = urlencode({"payload": '{"actions":{"0":{"action_id":"approve"}}}'}).encode()
    with pytest.raises(ValueError, match="at least one action"):
        Slack(signing_secret=SIGNING_SECRET)._parse_form(_Verified(body, int(NOW), {}))


def test_form_parser_enforces_the_64_field_cost_bound() -> None:
    body = urlencode({f"field_{number}": "x" for number in range(65)}).encode()
    with pytest.raises(ValueError, match="form body"):
        Slack(signing_secret=SIGNING_SECRET)._parse_form(_Verified(body, int(NOW), {}))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@hooks.slack.com/commands/T123/1/secret",
        "https://hooks.slack.com/commands/T123/1/secret?next=internal",
        "https://hooks.slack.com/commands/T123/1/secret#fragment",
        "https://hooks.slack.com/commands/T123",
        "https://hooks.slack.com/future/T123/1/secret",
    ],
)
def test_response_url_refuses_every_credential_suffix_and_path_escape(url: str) -> None:
    with pytest.raises(ValueError, match="response_url"):
        Slack(signing_secret=SIGNING_SECRET).response_url(url, installation="T123")


async def test_standard_origin_client_refuses_a_slack_gov_response_url() -> None:
    slack = Slack(signing_secret=SIGNING_SECRET, response_client=object())
    target = slack.response_url(
        "https://hooks.slack-gov.com/commands/T123/1/secret", installation="T123"
    )
    with pytest.raises(ValueError, match="pinned to hooks.slack.com"):
        await slack.respond(target, "no cross-origin send")


@pytest.mark.parametrize("method", ["", "chat/postMessage", "chat.postMessage?"])
async def test_web_api_refuses_invalid_method_names(method: str) -> None:
    with pytest.raises(ValueError, match="method"):
        await Slack(signing_secret=SIGNING_SECRET).call(installation(), method, {})


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ((500, {}, {"ok": False}), "HTTP 500"),
        ((200, {}, []), "non-object"),
        ((200, {}, {"ok": False, "error": "channel_not_found"}), "channel_not_found"),
    ],
)
async def test_web_api_refuses_each_unsuccessful_response(
    response: tuple[int, dict[str, str], object], message: str
) -> None:
    slack = Slack(
        signing_secret=SIGNING_SECRET,
        http_client=RecordingTransport([response]),
    )
    with pytest.raises(RuntimeError, match=message):
        await slack.call(installation(), "chat.postMessage", {})


async def test_web_api_stops_after_the_configured_retry_count() -> None:
    transport = RecordingTransport(
        [
            (429, {"retry-after": "0"}, {"ok": False}),
            (429, {"retry-after": "0"}, {"ok": False}),
        ]
    )
    slack = Slack(
        signing_secret=SIGNING_SECRET,
        http_client=transport,
        max_retries=1,
        sleep=lambda _delay: _done(),
    )
    with pytest.raises(SlackRateLimited):
        await slack.call(installation(), "chat.postMessage", {}, idempotent=True)
    assert len(transport.requests) == 2


async def _done() -> None:
    pass


async def test_outbound_call_without_any_transport_is_refused() -> None:
    with pytest.raises(RuntimeError, match="http_client"):
        await Slack(signing_secret=SIGNING_SECRET).call(installation(), "chat.postMessage", {})


class LegacyClientResponseTransport:
    async def request(
        self, _method: str, _url: str, *, headers: object, body: bytes
    ) -> ClientResponse:
        return ClientResponse(200, ((b"x-result", b"ok"),), b'{"ok":true}', "1.1")


async def test_legacy_transport_accepts_a_real_client_response_shape() -> None:
    result = await Slack(
        signing_secret=SIGNING_SECRET,
        http_client=LegacyClientResponseTransport(),
    ).call(installation(), "chat.postMessage", {})
    assert result == {"ok": True}


@pytest.mark.parametrize(
    "base_url",
    [
        "https:///missing-host",
        "https://user@ops.example",
        "https://user:secret@ops.example",
        "https://ops.example?query=yes",
        "https://ops.example#fragment",
    ],
)
def test_manifest_refuses_non_origin_base_urls(base_url: str) -> None:
    chat = ChatOps(name="ops", providers=(Slack(signing_secret=SIGNING_SECRET),))
    with pytest.raises(ValueError, match="absolute HTTPS origin"):
        chat.manifest("slack", base_url=base_url)


def test_task_names_are_bounded_valid_and_distinct_after_sanitizing() -> None:
    first = _task_name("operations/primary", "deploy-to-production" * 8)
    second = _task_name("operations.primary", "deploy-to-production" * 8)
    assert first != second
    assert len(first.encode()) <= 63
    assert all(character.isalnum() or character in "_$" for character in first)
    sanitized = _task_name("ops/primary", "deploy")
    long_safe = _task_name("o" * 60, "deploy")
    assert sanitized.startswith("chat_ops_primary_deploy_")
    assert long_safe.startswith("chat_") and len(long_safe) == 63
    assert _external_identity_payload(None) is None


def test_nested_required_identity_refuses_non_mapping_values() -> None:
    with pytest.raises(ValueError, match="nested identity must be an object"):
        _nested_text("T123", "id")


async def test_missing_content_type_is_refused_after_signature_verification() -> None:
    app = Wreath()
    ChatOps(
        app,
        name="ops",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
    )
    body = json_body({"type": "url_verification", "challenge": "challenge"})
    headers = signed_headers(body)
    del headers["content-type"]
    response = await TestClient(app).post("/chat/slack/events", content=body, headers=headers)
    assert response.status == 415


def context_for(inbound: _Inbound) -> ChatContext:
    return Slack(signing_secret=SIGNING_SECRET)._context(inbound)


def test_context_preserves_kind_empty_fallbacks_and_identity_absence() -> None:
    event = context_for(_Inbound("event", "app_mention", "T123", None, None, "E1", {}))
    assert (event.actor, event.conversation, event.command, event.action) == ("", "", None, None)
    assert event.inputs == {}
    assert event.external_identity is None

    action = context_for(_Inbound("action", "approve", "T123", "U123", "C123", "A1", {}, {"x": 1}))
    assert (action.command, action.action, action.inputs) == (None, "approve", {"x": 1})

    command = context_for(
        _Inbound("command", "deploy", "T123", "U123", "C123", "D1", {}, {"text": ""})
    )
    assert (command.command, command.action) == ("deploy", None)


async def test_configured_app_id_is_checked_without_an_installation_store() -> None:
    chat = SimpleNamespace(installations=None)
    inbound = _Inbound("event", "app_mention", "T123", "U1", "C1", "E1", {})
    slack = Slack(signing_secret=SIGNING_SECRET, app_id="A123")
    assert await slack._installation_key(chat, inbound) is None


async def test_installation_store_app_id_must_match_the_signed_envelope() -> None:
    class Store:
        async def fetch(self, **_facts: object) -> SlackInstallation:
            return installation(app_id="A-other")

    chat = SimpleNamespace(installations=None)
    inbound = _Inbound("event", "app_mention", "T123", "U1", "C1", "E1", {"api_app_id": "A123"})
    slack = Slack(signing_secret=SIGNING_SECRET, installations=Store())
    assert await slack._installation_key(chat, inbound) is None


async def test_installation_store_accepts_its_owner_when_envelope_omits_app_id() -> None:
    class Store:
        async def fetch(self, **_facts: object) -> SlackInstallation:
            return installation()

    inbound = _Inbound("event", "app_mention", "T123", "U1", "C1", "E1", {})
    slack = Slack(signing_secret=SIGNING_SECRET, installations=Store())
    assert await slack._installation_key(SimpleNamespace(installations=None), inbound) == "T123"


async def test_configured_inbox_owns_the_provider_claim() -> None:
    inbox = MemoryInbox()
    chat = ChatOps(name="ops", inbox=inbox)
    inbound = _Inbound("event", "app_mention", "T123", "U1", "C1", "E1", {})
    slack = Slack(signing_secret=SIGNING_SECRET)
    assert await slack._claim(chat, inbound)
    assert not await slack._claim(chat, inbound)
    assert inbox.atomic_claims == {("slack:T123", "E1")}


class LinkResolver:
    def __init__(self) -> None:
        self.return_to: str | None = None

    async def begin_link(self, _key: object, *, return_to: str) -> IdentityLinkChallenge:
        self.return_to = return_to
        return IdentityLinkChallenge("https://ops.example/link", "state")


async def test_link_reply_requires_both_link_capability_and_external_identity() -> None:
    slack = Slack(signing_secret=SIGNING_SECRET)
    current = context_for(_Inbound("command", "deploy", "T123", None, "C1", "D1", {}, {"text": ""}))
    identity_context = context_for(
        _Inbound("command", "deploy", "T123", "U1", "C1", "D1", {}, {"text": ""})
    )
    assert (
        await slack._link_reply(SimpleNamespace(identity=object()), identity_context)
    ).status == 403
    resolver = LinkResolver()
    assert (await slack._link_reply(SimpleNamespace(identity=resolver), current)).status == 403
    current.external_identity = identity_context.external_identity
    response = await slack._link_reply(SimpleNamespace(identity=resolver), current)
    assert response.status == 200
    assert resolver.return_to == ""
    current.response_url = "https://hooks.slack.com/commands/T123/1/secret"
    response = await slack._link_reply(SimpleNamespace(identity=resolver), current)
    assert response.status == 200
    assert resolver.return_to == current.response_url


def test_only_durable_commands_register_jobs_and_registration_is_idempotent() -> None:
    jobs = RecordingJobs()
    slack = Slack(signing_secret=SIGNING_SECRET)
    chat = ChatOps(name="ops", providers=(slack,), jobs=jobs, inbox=MemoryInbox())

    @chat.command("inline")
    async def inline() -> None:
        pass

    @chat.command("durable", execution="durable")
    async def durable() -> None:
        pass

    declaration = chat.commands["durable"]
    slack._register_command(chat, declaration)
    assert jobs.registered.keys() == {"chat_ops_durable"}


def test_durable_command_registration_preserves_an_explicit_retry_limit() -> None:
    class OptionJobs(RecordingJobs):
        def __init__(self) -> None:
            super().__init__()
            self.options: dict[str, object] = {}

        def task(self, name: str, **options: object):
            self.options = options
            return super().task(name)

    jobs = OptionJobs()
    chat = ChatOps(
        name="ops",
        providers=(Slack(signing_secret=SIGNING_SECRET),),
        jobs=jobs,
        inbox=MemoryInbox(),
    )

    @chat.command("durable", execution="durable", retries=2)
    async def durable() -> None:
        pass

    assert jobs.options == {"retries": 2}


def test_duplicate_durable_registration_does_not_touch_the_job_registry_twice() -> None:
    class CountingJobs(RecordingJobs):
        def __init__(self) -> None:
            super().__init__()
            self.registrations = 0

        def task(self, name: str):
            self.registrations += 1
            return super().task(name)

    jobs = CountingJobs()
    slack = Slack(signing_secret=SIGNING_SECRET)
    chat = ChatOps(name="ops", providers=(slack,), jobs=jobs, inbox=MemoryInbox())

    @chat.command("durable", execution="durable")
    async def durable() -> None:
        pass

    slack._register_command(chat, chat.commands["durable"])
    assert jobs.registrations == 1


async def test_durable_job_accepts_absent_actor_identity_and_response_url() -> None:
    jobs = RecordingJobs()
    slack = Slack(signing_secret=SIGNING_SECRET)
    chat = ChatOps(name="ops", providers=(slack,), jobs=jobs, inbox=MemoryInbox())
    seen: list[ChatContext] = []

    @chat.command("durable", execution="durable")
    async def durable(context: ChatContext) -> None:
        seen.append(context)

    current = context_for(
        _Inbound("command", "durable", "T123", None, "C1", "D1", {}, {"text": ""})
    )
    await slack._enqueue(
        chat,
        chat.commands["durable"],
        current,
        {},
        verified=_Verified(b"{}", int(NOW), {}),
    )
    task, payload, _key = jobs.enqueued[0]
    job = SimpleNamespace(
        job_id=7,
        tenant="slack:T123",
        key="D1",
        fence=1,
        trace_context=None,
        report=lambda *_: None,
    )
    await jobs.registered[task](job, payload)
    assert seen[0].external_identity is None
    assert seen[0].response_url is None


async def test_enqueue_without_jobs_refuses_at_the_provider_boundary() -> None:
    slack = Slack(signing_secret=SIGNING_SECRET)
    chat = ChatOps(name="ops")
    declaration = SimpleNamespace(name="deploy")
    current = context_for(_Inbound("command", "deploy", "T123", "U1", "C1", "D1", {}, {"text": ""}))
    with pytest.raises(RuntimeError, match="requires jobs"):
        await slack._enqueue(
            chat,
            declaration,
            current,
            {},
            verified=_Verified(b"{}", int(NOW), {}),
        )


def test_parser_refuses_an_unknown_media_type_directly() -> None:
    with pytest.raises(TypeError, match="application/json or form encoding"):
        Slack(signing_secret=SIGNING_SECRET)._parse(_Verified(b"", int(NOW), {}), "text/plain")


def test_signature_shape_checks_are_independent_and_canonical() -> None:
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    with pytest.raises(ValueError, match="canonical decimal"):
        slack._verify(
            b"",
            {
                "x-slack-request-timestamp": "١٨٠٠٠٠٠٠٠٠",
                "x-slack-signature": "v0=" + "0" * 64,
            },
        )
    with pytest.raises(ValueError, match="canonical decimal"):
        slack._verify(
            b"",
            {
                "x-slack-request-timestamp": "not-a-time",
                "x-slack-signature": "v0=" + "0" * 64,
            },
        )
    with pytest.raises(ValueError, match="v0 SHA-256"):
        slack._verify(
            b"",
            {
                "x-slack-request-timestamp": str(NOW),
                "x-slack-signature": "v1=" + "0" * 64,
            },
        )
    with pytest.raises(ValueError, match="v0 SHA-256"):
        slack._verify(
            b"",
            {
                "x-slack-request-timestamp": str(NOW),
                "x-slack-signature": "v0=short",
            },
        )


def test_command_argument_flag_forms_and_refusals_are_distinct() -> None:
    chat = ChatOps(name="ops")

    @chat.command("deploy")
    async def deploy(environment: str, force: bool = False) -> None:
        pass

    declaration = chat.commands["deploy"]
    assert _command_arguments(declaration, "--environment=production --force=false") == {
        "environment": "production",
        "force": "false",
    }
    with pytest.raises(ValueError, match="needs a value"):
        _command_arguments(declaration, "--environment")
    with pytest.raises(ValueError, match="invalid command text"):
        _command_arguments(declaration, "'unterminated")
    with pytest.raises(ValueError, match="unexpected command argument extra2"):
        _command_arguments(declaration, "--environment production extra extra2")


async def test_event_with_bot_metadata_is_still_an_event_not_a_bot_slash_command() -> None:
    app = Wreath()
    chat = ChatOps(
        app,
        name="ops",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
    )
    seen: list[str] = []

    @chat.event("app_mention")
    async def mention(context: ChatContext) -> None:
        seen.append(context.delivery_id)

    envelope = {
        "type": "event_callback",
        "team_id": "T123",
        "api_app_id": "A123",
        "bot_id": "B123",
        "event_id": "Ev-bot",
        "event": {"type": "app_mention", "user": "U123", "channel": "C123"},
    }
    body = json_body(envelope)
    response = await TestClient(app).post(
        "/chat/slack/events", content=body, headers=signed_headers(body)
    )
    assert response.status == 200
    assert seen == ["Ev-bot"]


async def test_action_binding_error_is_a_bad_request_not_a_command_reply() -> None:
    app = Wreath()
    chat = ChatOps(
        app,
        name="ops",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
    )

    @chat.action("approve")
    async def approve(required_reason: str) -> None:
        pass

    payload = {
        "type": "block_actions",
        "team": {"id": "T123"},
        "user": {"id": "U123"},
        "channel": {"id": "C123"},
        "actions": [{"action_id": "approve"}],
    }
    body = form_body(payload=json.dumps(payload, separators=(",", ":")))
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    response = await TestClient(app).post("/chat/slack/interactions", content=body, headers=headers)
    assert (response.status, response.body) == (400, b"")


def test_manifest_uses_command_name_when_description_is_absent() -> None:
    chat = ChatOps(name="ops", providers=(Slack(signing_secret=SIGNING_SECRET),))

    @chat.command("deploy")
    async def deploy() -> None:
        pass

    manifest = chat.manifest("slack", base_url="https://ops.example")
    assert manifest["features"]["slash_commands"][0]["description"] == "deploy"
