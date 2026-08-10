"""Cross-stack attacker corpora for Wreath's externally controlled boundaries.

The narrower subsystem suites prove individual contracts.  This file keeps the
release-level threat matrix executable: wire PoCs that used to be standalone
scripts, plus compact corpora for the parser, identity, egress, and filesystem
boundaries an Internet-facing application composes.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _metal import requires_metal

from wreath import Wreath
from wreath._graphql.parser import GraphQLSyntaxError, Limits, parse
from wreath.grpc import GrpcError, Status, Unframer, frame_message
from wreath.http_client import DestinationPolicy, DestinationRejected
from wreath.policy.sessions import SessionPolicy
from wreath.testing import TestClient

_ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    "script",
    sorted((_ROOT / "tests" / "security").glob("poc_*.py")),
    ids=lambda path: path.stem.removeprefix("poc_"),
)
@requires_metal
def test_standalone_proof_of_concept_is_a_collected_regression(script: Path) -> None:
    """A PoC must reach its explicit safe outcome, not merely exit non-zero."""
    environment = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=_ROOT,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
        check=False,
    )
    transcript = completed.stdout + completed.stderr
    assert completed.returncode == 1, transcript
    assert "not vulnerable" in transcript.lower(), transcript
    assert "VULNERABLE:" not in transcript, transcript


_NON_GLOBAL_ADDRESSES = (
    "0.0.0.0",
    "0.0.0.1",
    "10.0.0.1",
    "10.255.255.254",
    "100.64.0.1",
    "127.0.0.1",
    "127.255.255.254",
    "169.254.1.1",
    "172.16.0.1",
    "172.31.255.254",
    "192.0.0.1",
    "192.0.2.1",
    "192.168.0.1",
    "198.18.0.1",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",
    "239.255.255.255",
    "255.255.255.255",
    "::",
    "::1",
    "fe80::1",
    "fe80::1%eth0",
    "ff02::1",
    "2001:db8::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
    "::127.0.0.1",
    "::169.254.169.254",
)


@pytest.mark.parametrize("address", _NON_GLOBAL_ADDRESSES)
def test_default_egress_policy_refuses_non_global_and_translated_addresses(
    address: str,
) -> None:
    with pytest.raises(DestinationRejected):
        DestinationPolicy().validate_address(address)


@pytest.mark.parametrize(
    "address",
    (
        "1.1.1.1",
        "8.8.8.8",
        "2001:4860:4860::8888",
        "2606:4700:4700::1111",
        "::1.1.1.1",
        "64:ff9b::101:101",
    ),
)
def test_default_egress_policy_preserves_public_destinations(address: str) -> None:
    DestinationPolicy().validate_address(address)


def _flip_cookie_character(cookie: str, index: int) -> str:
    replacement = "A" if cookie[index] != "A" else "B"
    return cookie[:index] + replacement + cookie[index + 1 :]


@pytest.mark.parametrize("fraction", tuple(range(25)))
def test_signed_session_cookie_rejects_tampering_across_its_whole_shape(
    fraction: int,
) -> None:
    middleware = SessionPolicy(secret="s" * 32, secure=False)
    cookie = middleware._sign(b'{"principal":{"sub":"victim"}}', int(time.time()))
    index = fraction * (len(cookie) - 1) // 24

    assert middleware._load(_flip_cookie_character(cookie, index)) is None


@pytest.mark.parametrize(
    "value",
    (
        "",
        ".",
        "..",
        "...",
        "body.stamp",
        "body.stamp.mac.extra",
        "body.not-a-time.mac",
        "====.0." + "0" * 64,
        "body.0." + "0" * 63,
        "body.0." + "0" * 65,
    ),
)
def test_signed_session_cookie_refuses_malformed_envelopes(value: str) -> None:
    middleware = SessionPolicy(secret="s" * 32, secure=False)
    assert middleware._load(value) is None


_STATIC_TRAVERSALS = (
    "/assets/../secret.txt",
    "/assets/../../secret.txt",
    "/assets/%2e%2e/secret.txt",
    "/assets/%2E%2E/secret.txt",
    "/assets/.%2e/secret.txt",
    "/assets/%2e./secret.txt",
    "/assets/%252e%252e/secret.txt",
    "/assets/%2e%2e%2fsecret.txt",
    "/assets/%2e%2e%5csecret.txt",
    "/assets/..%2fsecret.txt",
    "/assets/..%5csecret.txt",
    "/assets//../secret.txt",
    "/assets/a/../../secret.txt",
    "/assets/a/%2e%2e/%2e%2e/secret.txt",
)


@pytest.mark.parametrize("path", _STATIC_TRAVERSALS)
async def test_static_mount_never_serves_a_sibling_through_traversal_spellings(
    tmp_path: Path, path: str
) -> None:
    public = tmp_path / "public"
    public.mkdir()
    (public / "index.txt").write_text("public", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("release-secret", encoding="utf-8")
    app = Wreath()
    app.static("/assets", str(public))

    async with TestClient(app) as client:
        response = await client.get(path)

    assert response.status in {400, 404}
    assert b"release-secret" not in response.body


def _nested_query(levels: int) -> str:
    return "{" + "node{" * levels + "leaf" + "}" * levels + "}"


@pytest.mark.parametrize("levels", (1, 2, 3, 5, 8, 13, 21))
def test_graphql_accepts_a_query_exactly_at_its_depth_limit(levels: int) -> None:
    document = parse(_nested_query(levels), Limits(max_depth=levels + 1))
    assert document.depth == levels + 1


@pytest.mark.parametrize("levels", (1, 2, 3, 5, 8, 13, 21))
def test_graphql_refuses_a_query_one_level_past_its_depth_limit(levels: int) -> None:
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse(_nested_query(levels), Limits(max_depth=levels))
    assert caught.value.code == "depth"


def _flat_query(fields: int) -> str:
    return "{ " + " ".join(f"field{index}" for index in range(fields)) + " }"


@pytest.mark.parametrize("fields", (1, 2, 4, 8, 16, 32, 64, 128))
def test_graphql_accepts_complexity_exactly_at_the_limit(fields: int) -> None:
    document = parse(_flat_query(fields), Limits(max_complexity=fields))
    assert document.complexity == fields


@pytest.mark.parametrize("fields", (2, 4, 8, 16, 32, 64, 128))
def test_graphql_refuses_complexity_one_field_past_the_limit(fields: int) -> None:
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse(_flat_query(fields), Limits(max_complexity=fields - 1))
    assert caught.value.code == "complexity"


@pytest.mark.parametrize("limit", (1, 2, 7, 31, 255, 4096))
def test_grpc_accepts_an_uncompressed_message_exactly_at_its_limit(limit: int) -> None:
    payload = b"x" * limit
    assert Unframer(max_message_bytes=limit).feed(frame_message(payload)) == [payload]


@pytest.mark.parametrize("limit", (1, 2, 7, 31, 255, 4096))
def test_grpc_refuses_an_oversized_declared_length_before_receiving_a_body(
    limit: int,
) -> None:
    prefix = b"\x00" + (limit + 1).to_bytes(4, "big")
    with pytest.raises(GrpcError) as caught:
        Unframer(max_message_bytes=limit).feed(prefix)
    assert caught.value.status is Status.RESOURCE_EXHAUSTED


@pytest.mark.parametrize("limit", (32, 128, 1024, 8192))
def test_grpc_refuses_a_compressed_message_that_expands_past_the_limit(
    limit: int,
) -> None:
    compressed = gzip.compress(b"x" * (limit + 1), mtime=0)
    wire = frame_message(compressed, compressed=True)
    with pytest.raises(GrpcError) as caught:
        Unframer(max_message_bytes=limit, encoding="gzip").feed(wire)
    assert caught.value.status is Status.RESOURCE_EXHAUSTED


@pytest.mark.parametrize("flag", (2, 3, 127, 255))
def test_grpc_refuses_compressed_flag_values_other_than_zero_or_one(flag: int) -> None:
    compressed = gzip.compress(b"safe", mtime=0)
    wire = bytes((flag,)) + len(compressed).to_bytes(4, "big") + compressed
    with pytest.raises(GrpcError) as caught:
        Unframer(max_message_bytes=1024, encoding="gzip").feed(wire)
    assert caught.value.status is Status.INTERNAL
