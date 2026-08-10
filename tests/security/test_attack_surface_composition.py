"""Stateful and compositional attacks against an Internet-facing Wreath stack.

``test_attack_surface_matrix`` concentrates on hostile bytes at one boundary.
This companion concentrates on what changes across requests: principals share
workers, work is cancelled, retries race, connections resolve more than one
address, tenant names reach SQL setup, and operator tooling writes artifacts.

The deep protocol/environment proofs remain in their owned suites: real proxy
and HTTP parser differentials in ``test_server_fuzz``/``test_proxy_middleware``;
disconnect-to-PostgreSQL cancellation in ``test_disconnect_cancels_query``;
TOTP/WebAuthn races in ``test_users_totp``/``test_users_webauthn``; HTTP/MCP
authorization equivalence in ``test_mcp_expose_routes``/``test_red_team_mcp``;
capture redaction in ``test_flight_capture``/``test_mcp_recording``; and real
QUIC requests in ``tests/http3/test_adversarial.py``.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from typing import Any

import pytest
from wreath._native._core import TrustedNetworks

from wreath import Wreath
from wreath._secondfactor import MemoryChallengeStore
from wreath.auth import authenticated
from wreath.crud import SENSITIVE_FIELD
from wreath.grpc import Unframer, frame_message
from wreath.http_client import (
    DestinationRejected,
    HTTPClient,
    RedirectError,
)
from wreath.orm import TenantContext
from wreath.orm.errors import DeclarationError
from wreath.policy import MemoryIdempotencyStore
from wreath.policy.sessions import SessionPolicy
from wreath.request import RequestLimits
from wreath.response import Response
from wreath.response_cache import cached
from wreath.testing import TestClient
from wreath.typegen.cli import TypegenCliError, write
from wreath.webhooks import LocalReplayStore


class _CachedRequest:
    method = "GET"
    path = "/account"
    query_string = b""

    def __init__(self, principal: str) -> None:
        self.identity = principal


class _PublicCachedRequest:
    method = "GET"
    path = "/public"
    query_string = b""


_PRINCIPAL_PAIRS = (
    ("ada", "bo"),
    ("1", "01"),
    ("User::1", "Service::1"),
    ("a:b", "a/b"),
    ("a b", "a+b"),
    ("a%2Fb", "a/b"),
    ("admin", "admin\x00suffix"),
    ("é", "e\N{COMBINING ACUTE ACCENT}"),
    ("Ａ", "A"),
    ("user@example.com", "user@example.com.evil"),
    ("tenant-a/user", "tenant-b/user"),
    ("anonymous", ""),
)


@pytest.mark.parametrize(("victim", "attacker"), _PRINCIPAL_PAIRS)
async def test_shared_response_cache_never_serves_one_principal_to_another(
    victim: str, attacker: str
) -> None:
    calls: list[str] = []

    @cached(ttl=60)
    async def account(request: _CachedRequest) -> Response:
        calls.append(request.identity)
        return Response(request.identity.encode("utf-8"))

    victim_response = await account(_CachedRequest(victim))
    attacker_response = await account(_CachedRequest(attacker))

    assert victim_response.body == victim.encode()
    assert attacker_response.body == attacker.encode()
    assert calls == [victim, attacker]
    assert account.cache_store.stats.hits == 0


async def test_concurrent_authenticated_requests_keep_task_local_identities() -> None:
    app = Wreath()
    rendezvous = asyncio.Barrier(2)

    @app.get("/who")
    @authenticated()
    async def who(request: Any) -> dict[str, str]:
        await rendezvous.wait()
        return {"id": request.identity.id}

    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        bo = client.acting_as("bo")
        answers = await asyncio.gather(ada.get("/who"), bo.get("/who"))

    assert {answer.json()["id"] for answer in answers} == {"ada", "bo"}


async def test_identified_request_can_use_a_key_that_names_its_principal() -> None:
    calls = 0

    @cached(ttl=60, key=lambda request: request.identity)
    async def account(request: _CachedRequest) -> Response:
        nonlocal calls
        calls += 1
        return Response(request.identity.encode())

    assert (await account(_CachedRequest("ada"))).body == b"ada"
    assert (await account(_CachedRequest("ada"))).body == b"ada"
    assert calls == 1
    assert account.cache_store.stats.hits == 1


@pytest.mark.parametrize("other_byte", range(65, 77))
def test_session_cookie_from_another_application_secret_is_refused(
    other_byte: int,
) -> None:
    issued_by = SessionPolicy(secret="A" * 32, secure=False)
    read_by = SessionPolicy(secret=chr(other_byte) * 32, secure=False)
    cookie = issued_by._sign(b'{"principal":"victim"}', int(time.time()))

    if other_byte == ord("A"):
        loaded = read_by._load(cookie)
        assert loaded is not None and loaded[0] == {"principal": "victim"}
    else:
        assert read_by._load(cookie) is None


async def test_cancelled_cache_owner_releases_waiters_and_the_key() -> None:
    started = asyncio.Event()
    hold = asyncio.Event()
    calls = 0

    @cached(ttl=60)
    async def slow(request: Any) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await hold.wait()
        return Response(b"recovered")

    owner = asyncio.create_task(slow(_PublicCachedRequest()))
    await asyncio.wait_for(started.wait(), timeout=1)
    waiter = asyncio.create_task(slow(_PublicCachedRequest()))
    await asyncio.sleep(0)
    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert (await slow(_PublicCachedRequest())).body == b"recovered"
    assert calls == 2


async def test_raising_cache_owner_releases_waiters_and_the_key() -> None:
    started = asyncio.Event()
    hold = asyncio.Event()
    calls = 0

    @cached(ttl=60)
    async def unstable(request: Any) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await hold.wait()
            raise RuntimeError("first computation failed")
        return Response(b"recovered")

    owner = asyncio.create_task(unstable(_PublicCachedRequest()))
    await asyncio.wait_for(started.wait(), timeout=1)
    waiter = asyncio.create_task(unstable(_PublicCachedRequest()))
    await asyncio.sleep(0)
    hold.set()

    for task in (owner, waiter):
        with pytest.raises(RuntimeError, match="first computation failed"):
            await task

    assert (await unstable(_PublicCachedRequest())).body == b"recovered"
    assert calls == 2


async def test_cold_cache_key_does_not_wait_for_a_nonexistent_owner() -> None:
    @cached(ttl=60)
    async def immediate(request: Any) -> Response:
        return Response(b"owner")

    response = await asyncio.wait_for(immediate(_PublicCachedRequest()), timeout=1)
    assert response.body == b"owner"


@pytest.mark.parametrize("racers", (2, 3, 8, 32, 128))
async def test_idempotency_claim_has_exactly_one_winner(racers: int) -> None:
    store = MemoryIdempotencyStore()
    outcomes = await asyncio.gather(*(store.reserve("same-key") for _ in range(racers)))
    assert sum(outcome == ("fresh", None) for outcome in outcomes) == 1
    assert sum(outcome == ("in_flight", None) for outcome in outcomes) == racers - 1


@pytest.mark.parametrize("racers", (2, 3, 8, 32, 128))
async def test_webhook_replay_claim_has_exactly_one_winner(racers: int) -> None:
    store = LocalReplayStore(max_entries=256, ttl=60)
    outcomes = await asyncio.gather(
        *(store.claim("sender", "same-event", now=100.0) for _ in range(racers))
    )
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == racers - 1


@pytest.mark.parametrize("racers", (2, 3, 8, 32, 128))
async def test_challenge_consumption_has_exactly_one_winner(racers: int) -> None:
    store = MemoryChallengeStore()
    await store.put(
        "same-handle",
        user_id="victim",
        kind="webauthn",
        payload={"challenge": "one-use"},
        ttl=60,
    )
    outcomes = await asyncio.gather(
        *(
            store.consume("same-handle", user_id="victim", kind="webauthn")
            for _ in range(racers)
        )
    )
    assert outcomes.count({"challenge": "one-use"}) == 1
    assert outcomes.count(None) == racers - 1


_CROSS_ORIGIN_REDIRECTS = (
    b"//evil.example/path",
    b"http://example.com/path",
    b"https://evil.example/path",
    b"https://example.com.evil/path",
    b"https://example.com@evil.example/path",
    b"https://evil.example@example.com/path",
    b"https://example.com:444/path",
    b"https://127.0.0.1/path",
    b"https://[::1]/path",
    b"https://169.254.169.254/latest/meta-data/",
    b"https://example.com%2eevil.example/path",
    b"https://example.com./path",
)


@pytest.mark.parametrize("location", _CROSS_ORIGIN_REDIRECTS)
def test_redirect_canonicalization_never_leaves_the_configured_origin(
    location: bytes,
) -> None:
    client = HTTPClient("redirect-policy", base_url="https://example.com/base")
    with pytest.raises((RedirectError, DestinationRejected, ValueError)):
        client._redirect_target("/base/start", location)


@pytest.mark.parametrize(
    ("location", "expected"),
    (
        (b"/next", "/next"),
        (b"next", "/base/next"),
        (b"../next", "/next"),
        (b"?page=2", "/base/start?page=2"),
        (b"https://example.com/next", "/next"),
        (b"https://example.com:443/next?q=1", "/next?q=1"),
    ),
)
def test_redirect_canonicalization_preserves_same_origin_controls(
    location: bytes, expected: str
) -> None:
    client = HTTPClient("redirect-policy", base_url="https://example.com/base")
    assert client._redirect_target("/base/start", location) == expected


async def test_one_private_dns_answer_refuses_the_whole_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def mixed_answers(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", mixed_answers)
    client = HTTPClient("mixed-dns", base_url="http://example.com")
    with pytest.raises(DestinationRejected):
        await client._resolve()
    assert client._dns_addresses == ()


_MALICIOUS_TENANT_IDENTIFIERS = (
    "",
    "1tenant",
    "tenant-name",
    "tenant.name",
    "tenant/name",
    "tenant\\name",
    "tenant name",
    "tenant\tname",
    "tenant\nname",
    "tenant\x00name",
    'tenant"name',
    "tenant'name",
    "tenant;drop schema public",
    "tenant --",
    "tenant/*x*/",
    "tenant,public",
    "public.pg_catalog",
    "$user",
    "étenant",
    "Ａtenant",
)


@pytest.mark.parametrize("value", _MALICIOUS_TENANT_IDENTIFIERS)
def test_tenant_context_refuses_identifier_injection(value: str) -> None:
    with pytest.raises(DeclarationError, match="tenant schema"):
        TenantContext(schema=value)
    with pytest.raises(DeclarationError, match="tenant role"):
        TenantContext(schema="safe_tenant", role=value)


@pytest.mark.parametrize("value", (None, b"tenant", 7, 7.0, object()))
def test_tenant_context_refuses_non_string_identifiers(value: Any) -> None:
    with pytest.raises(DeclarationError, match="tenant schema"):
        TenantContext(schema=value)
    if value is not None:
        with pytest.raises(DeclarationError, match="tenant role"):
            TenantContext(schema="safe_tenant", role=value)


@pytest.mark.parametrize(
    "field",
    (
        "max_body_bytes",
        "max_parts",
        "max_part_header_bytes",
        "max_part_bytes",
        "max_form_memory_bytes",
        "spool_max_bytes",
        "max_cookie_bytes",
        "max_form_fields",
    ),
)
@pytest.mark.parametrize("value", (0, -1))
def test_every_request_resource_limit_refuses_non_positive_values(
    field: str, value: int
) -> None:
    with pytest.raises(ValueError, match=field):
        RequestLimits(**{field: value})


@pytest.mark.parametrize("size", (0, 1, 2, 7, 31, 255, 4096))
def test_grpc_unframer_survives_every_one_byte_boundary(size: int) -> None:
    payload = bytes(index % 251 for index in range(size))
    unframer = Unframer(max_message_bytes=max(1, size))
    messages: list[bytes] = []
    for byte in frame_message(payload):
        messages.extend(unframer.feed(bytes((byte,))))
    unframer.finish()
    assert messages == [payload]


_EXCEPTION_CANARIES = (
    "password=correct-horse-battery-staple",
    "authorization: Bearer release-token",
    "cookie: wreath_session=private",
    "postgresql://user:secret@database/app",
    "AWS_SECRET_ACCESS_KEY=secret",
    "-----BEGIN PRIVATE KEY-----",
    "mcp.arg.password=hunter2",
    "tenant/customer-42/internal",
)


@pytest.mark.parametrize("canary", _EXCEPTION_CANARIES)
async def test_production_problem_response_never_echoes_exception_secrets(
    canary: str,
) -> None:
    app = Wreath(debug=False)

    @app.get("/boom")
    async def boom(request: Any) -> Response:
        raise RuntimeError(canary)

    async with TestClient(app) as client:
        response = await client.get("/boom")

    assert response.status == 500
    assert canary.encode() not in response.body
    assert b"RuntimeError" not in response.body


async def test_debug_problem_response_explicitly_exposes_the_developer_error() -> None:
    app = Wreath(debug=True)
    canary = "debug-only-diagnostic"

    @app.get("/boom")
    async def boom(request: Any) -> Response:
        raise RuntimeError(canary)

    async with TestClient(app) as client:
        response = await client.get("/boom")

    assert response.status == 500
    assert canary.encode() in response.body
    assert b"RuntimeError" in response.body


_SENSITIVE_NAMES = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "secret_key",
    "client_secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "authorization",
    "cookie",
    "session_token",
    "private_key",
    "recovery_code",
    "backup_code",
    "totp_secret",
    "webauthn_secret",
    "credential",
)


@pytest.mark.parametrize("name", _SENSITIVE_NAMES)
def test_common_secret_names_are_classified_for_redaction(name: str) -> None:
    assert SENSITIVE_FIELD.search(name) is not None


@pytest.mark.parametrize(
    "name",
    (
        "",
        ".",
        "./",
        "..",
        "../victim.py",
        "../../victim.py",
        "sub/../../victim.py",
        "/tmp/wreath-typegen-victim.py",
    ),
)
def test_typegen_refuses_generated_names_outside_its_output_directory(
    tmp_path: Path, name: str
) -> None:
    output = tmp_path / "generated"
    sibling = tmp_path / "victim.py"
    sibling.write_text("owner-data", encoding="utf-8")

    with pytest.raises(TypegenCliError, match="generated path"):
        write({name: "attacker-data"}, output)

    assert sibling.read_text(encoding="utf-8") == "owner-data"
    assert not (tmp_path / "generated.tmp").exists()


def test_typegen_temporary_file_cannot_be_preplanted_as_a_symlink(
    tmp_path: Path,
) -> None:
    output = tmp_path / "generated"
    output.mkdir()
    victim = tmp_path / "victim.py"
    victim.write_text("owner-data", encoding="utf-8")
    (output / "client.ts.tmp").symlink_to(victim)

    write({"client.ts": "generated-data"}, output)

    assert victim.read_text(encoding="utf-8") == "owner-data"
    assert (output / "client.ts").read_text(encoding="utf-8") == "generated-data"


@pytest.mark.parametrize(
    "value",
    (
        b"",
        b"unknown",
        b"for=203.0.113.1",
        b"010.000.000.001",
        b"[2001:db8::1",
        b"2001:db8::1]",
        b"[fe80::1%eth0]",
        b"203.0.113.999:443",
        b"203.0.113.1\x00, 10.0.0.1",
        b"203.0.113.1\r\nX-Evil: yes",
    ),
)
def test_malformed_forwarded_chain_cannot_replace_the_socket_peer(value: bytes) -> None:
    trusted = TrustedNetworks(("10.0.0.0/8",))
    assert trusted.forwarded_client(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (b"203.0.113.1", "203.0.113.1"),
        (b"203.0.113.1, 10.0.0.1", "203.0.113.1"),
        (b"[2001:db8::1]:443, 10.0.0.1", "2001:db8::1"),
        (b"10.0.0.1, 10.0.0.2", "10.0.0.1"),
    ),
)
def test_valid_forwarded_chain_still_resolves_the_client(
    value: bytes, expected: str
) -> None:
    trusted = TrustedNetworks(("10.0.0.0/8",))
    assert trusted.forwarded_client(value) == expected
