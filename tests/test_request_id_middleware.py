"""RequestIdPolicy: inbound validation, minting, and echo."""

from __future__ import annotations

import os
import re
from typing import Any

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath.policy import HttpPolicy, RequestIdPolicy, request_id
from wreath.testing import TestClient

validator = _core.request_id_valid


def test_validator_accepts_correlation_ids_and_rejects_injection() -> None:
    for value, expected in (
        (b"0123456789abcdef", True),
        (b"4bf92f3577b34da6a3ce929d0e0e4736", True),  # W3C trace id
        (b"550e8400-e29b-41d4-a716-446655440000", True),  # UUID
        (b"a.b_c-d", True),
        (b"", False),
        (b"has space", False),
        (b"newline\ninjected", False),
        (b"semi;colon", False),
        (b"<script>", False),
        (b"quote\"", False),
        (b"\x00null", False),
        (b"caf\xc3\xa9", False),  # non-ASCII
    ):
        assert validator(value, 128) is expected, value
    assert validator(b"x" * 128, 128) is True
    assert validator(b"x" * 129, 128) is False


def test_validator_applies_the_same_charset_under_a_shorter_bound() -> None:
    """The charset and the length bound are separate rejections.

    A 64-byte bound rather than 128, because the length check and the charset
    check are separate rejections and a validator that conflated them would look
    correct at a single limit. `b"-" * 200` is the one that separates them: every
    byte is in the charset, so only the bound can refuse it.
    """
    for value, expected in (
        (b"abc", True),
        (b"a.b-c_d", True),
        (b"A1", True),  # upper case is in the charset
        (b"", False),
        (b"a b", False),
        (b"-" * 200, False),  # charset-clean, refused on length alone
        (b"\xff", False),
    ):
        assert validator(value, 64) is expected, value


async def test_valid_inbound_id_is_reused_and_echoed() -> None:
    app = Wreath(
        http_policy=HttpPolicy(request_id=RequestIdPolicy(trust_inbound=True))
    )
    seen: list[str] = []

    @app.get("/")
    async def index(request: Any) -> str:
        seen.append(request_id(request))
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/", headers={"x-request-id": "trace-abc-123"})

    assert seen == ["trace-abc-123"]
    assert response.header("x-request-id") == "trace-abc-123"


async def test_hostile_inbound_id_is_replaced_not_sanitized() -> None:
    app = Wreath(
        http_policy=HttpPolicy(request_id=RequestIdPolicy(trust_inbound=True))
    )

    @app.get("/")
    async def index(request: Any) -> str:
        return request_id(request)

    async with TestClient(app) as client:
        response = await client.get("/", headers={"x-request-id": "evil value;with junk"})

    echoed = response.header("x-request-id")
    assert echoed is not None
    assert "evil" not in echoed
    assert len(echoed) == 32  # freshly minted
    assert response.body.decode() == echoed


async def test_inbound_id_is_distrusted_by_default() -> None:
    app = Wreath(http_policy=HttpPolicy(request_id=RequestIdPolicy()))

    @app.get("/")
    async def index(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/", headers={"x-request-id": "perfectly-valid"})

    assert response.header("x-request-id") != "perfectly-valid"


async def test_ids_are_minted_per_request_and_can_go_unechoed() -> None:
    app = Wreath(
        http_policy=HttpPolicy(request_id=RequestIdPolicy(echo=False))
    )
    seen: list[str] = []

    @app.get("/")
    async def index(request: Any) -> str:
        seen.append(request_id(request))
        return "ok"

    async with TestClient(app) as client:
        first = await client.get("/")
        await client.get("/")

    assert first.header("x-request-id") is None
    assert len(set(seen)) == 2


async def test_id_covers_responses_the_router_never_reached() -> None:
    app = Wreath(http_policy=HttpPolicy(request_id=RequestIdPolicy()))

    async with TestClient(app) as client:
        response = await client.get("/nope")

    assert response.status == 404
    assert response.header("x-request-id") is not None


def test_request_id_without_the_middleware_is_an_error() -> None:
    from wreath.request import Request

    with pytest.raises(RuntimeError, match="has not assigned an id"):
        request_id(Request({"type": "http", "headers": []}, None))


def test_configuration_is_validated() -> None:
    with pytest.raises(ValueError):
        RequestIdPolicy(header="")
    with pytest.raises(ValueError):
        RequestIdPolicy(max_length=0)


@pytest.mark.skipif(
    _core is None or not hasattr(_core, "random_hex"),
    reason="native _core is not built",
)
class TestNativeRandomHex:
    """`_core.random_hex` replaced `os.urandom(n).hex()` on the mint path.

    `os.urandom` performs a real syscall per call; the C twin draws through
    `getrandom(2)`, which glibc answers from the vDSO. That is an 11x
    difference on a hook that runs for every request of every application that
    installs this middleware -- but it is also a *different source of
    randomness*, so the properties the identifier depends on are asserted here
    rather than assumed from the speedup.
    """

    def test_it_produces_lowercase_hex_of_the_requested_length(self) -> None:
        for size in (1, 8, 16, 32, 64):
            value = _core.random_hex(size)
            assert len(value) == size * 2
            assert re.fullmatch(r"[0-9a-f]+", value)

    def test_it_does_not_repeat(self) -> None:
        """Correlation ids collide silently: two requests become one in a trace."""
        drawn = {_core.random_hex(16) for _ in range(5000)}
        assert len(drawn) == 5000

    def test_it_refuses_a_size_its_buffer_cannot_hold(self) -> None:
        """A stack buffer bounds this; the bound is checked, not assumed."""
        for size in (0, -1, 65, 1 << 20):
            with pytest.raises(ValueError):
                _core.random_hex(size)

    def test_random_hex_renders_lowercase_hex_two_chars_per_byte(self) -> None:
        """The shape `os.urandom(n).hex()` produces, which is what callers parse."""
        assert re.fullmatch(r"[0-9a-f]{32}", _core.random_hex(16))
        assert len(_core.random_hex(16)) == len(os.urandom(16).hex())
