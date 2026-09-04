"""A minimal DNS TXT client, because the stdlib has no resolver.

`socket.getaddrinfo` answers A and AAAA and nothing else, so a check that wants
to read an SPF, DKIM or DMARC record has no stdlib route to it at all -- and
those three records are the difference between mail arriving and mail being
rejected with a permanent 550. Rather than take a runtime dependency for one
query type, this builds the query and parses the answer: RFC 1035 §4, restricted
to `IN TXT`.

Deliberately small, and deliberately not a resolver:

* **TXT only.** No A, no MX, no SRV, no CNAME chasing beyond what the server
  already put in the answer section.
* **No caching**, because the only caller is a diagnostic that runs when someone
  asks.
* **No DNSSEC validation.** The answer is used to *report* a configuration
  problem, never to make an authorization decision, and that distinction is
  what makes an unvalidated answer acceptable here. Do not reuse this module for
  anything that decides.
* **UDP with a TCP retry** when the answer is truncated, which SPF records with
  many `include:` terms routinely are.

Every failure is a return value rather than an exception: a check that crashes
because a nameserver is slow is worse than a check that says it could not tell.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import secrets
import socket
import struct
from dataclasses import dataclass

from ._native import _core

__all__ = ["DnsAnswer", "resolve_txt"]

_TYPE_TXT = 16
_CLASS_IN = 1
#: RFC 1035 §2.3.4: 255 octets for a name, 63 per label.
_MAX_NAME = 255
_MAX_LABEL = 63
#: Enough for any TXT answer over TCP; a larger one is a server we do not want
#: to read anyway.
_MAX_RESPONSE = 65535
_MAX_NAMESERVERS = 3
_MAX_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class DnsAnswer:
    """The outcome of one TXT lookup.

    `records` holds each TXT record with its character-strings already joined,
    which is what a long DKIM public key arrives as. `error` is set when the
    lookup could not be completed -- distinct from a successful lookup that
    found nothing, which is `records == ()` and `error is None`.
    """

    name: str
    records: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise TypeError(f"DNS answer name must be a string, got {self.name!r}")
        if not isinstance(self.records, tuple):
            object.__setattr__(self, "records", tuple(self.records))
        if any(type(record) is not str for record in self.records):
            raise TypeError("DNS answer records must contain only strings")
        if self.error is not None and type(self.error) is not str:
            raise TypeError(f"DNS answer error must be a string or None, got {self.error!r}")

    @property
    def resolved(self) -> bool:
        """Whether an answer was obtained at all, empty or not."""
        return self.error is None


def _nameservers() -> list[str]:
    """Nameservers from `WREATH_DNS_SERVER`, then `/etc/resolv.conf`.

    The environment variable exists so a test can point this at a local stub
    without editing a system file, and so a container with no `resolv.conf` can
    be told where to look.
    """
    override = os.environ.get("WREATH_DNS_SERVER")
    if override:
        return [server.strip() for server in override.split(",") if server.strip()]
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="ascii", errors="replace") as handle:
            for line in handle:
                match = re.match(r"\s*nameserver\s+(\S+)", line)
                if match:
                    servers.append(match.group(1))
    except OSError:
        return []
    return servers


def _encode_name(name: str) -> bytes:
    if type(name) is not str:
        raise ValueError(f"DNS name must be a string, got {name!r}")
    if len(name) > _MAX_NAME:
        raise ValueError(f"a DNS name is at most {_MAX_NAME} octets on the wire")
    if any(ord(character) < 33 or ord(character) == 127 for character in name):
        raise ValueError(f"DNS name must contain non-control IDNA text, got {name!r}")
    out = bytearray()
    absolute = name[:-1] if name.endswith(".") else name
    for label in absolute.split("."):
        try:
            encoded = label.encode("idna") if not label.isascii() else label.encode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"invalid DNS label {label!r}; expected IDNA text") from exc
        if not encoded or len(encoded) > _MAX_LABEL:
            raise ValueError(f"invalid DNS label {label!r}")
        out.append(len(encoded))
        out += encoded
    out.append(0)
    if len(out) > _MAX_NAME:
        raise ValueError(f"a DNS name is at most {_MAX_NAME} octets on the wire")
    return bytes(out)


def _decode_dns_name(message: bytes, start: int) -> tuple[tuple[bytes, ...], int]:
    labels: list[bytes] = []
    cursor = start
    end: int | None = None
    pointers: set[int] = set()
    while True:
        if cursor >= len(message):
            raise ValueError("truncated DNS question name")
        size = message[cursor]
        if size & 0xC0 == 0xC0:
            if cursor + 1 >= len(message):
                raise ValueError("truncated DNS question compression pointer")
            pointer = ((size & 0x3F) << 8) | message[cursor + 1]
            if pointer in pointers or len(pointers) >= 128:
                raise ValueError("cyclic DNS question compression pointer")
            pointers.add(pointer)
            if end is None:
                end = cursor + 2
            cursor = pointer
            continue
        if size & 0xC0:
            raise ValueError("invalid DNS question label")
        cursor += 1
        if size == 0:
            return tuple(labels), end if end is not None else cursor
        if cursor > len(message) - size:
            raise ValueError("truncated DNS question label")
        labels.append(message[cursor : cursor + size].lower())
        cursor += size


def _parse(
    response: bytes, query_id: int, expected_name: bytes | None = None
) -> tuple[str, ...]:
    if len(response) >= 6:
        flags, questions = struct.unpack_from("!HH", response, 2)
        if not flags & 0x8000 or flags & 0x7800 or flags & 0x0200:
            raise ValueError("DNS packet is not a complete standard response")
        if questions != 1:
            raise ValueError("DNS response must contain exactly one question")
        response_name, question_end = _decode_dns_name(response, 12)
        if question_end > len(response) - 4:
            raise ValueError("truncated DNS question")
        if struct.unpack_from("!HH", response, question_end) != (_TYPE_TXT, _CLASS_IN):
            raise ValueError("DNS response question must be IN TXT")
        if expected_name is not None:
            query_name, _ = _decode_dns_name(expected_name, 0)
            if response_name != query_name:
                raise ValueError("DNS response question name does not match the query")
    return _core.dns_parse_txt(response, query_id)


def _nameserver_family(address: object) -> socket.AddressFamily:
    if type(address) is not str or not address:
        raise ValueError(f"nameserver must be an IPv4 or IPv6 address, got {address!r}")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError(
            f"nameserver must be an IPv4 or IPv6 address, got {address!r}"
        ) from exc
    return socket.AF_INET if parsed.version == 4 else socket.AF_INET6


def _same_endpoint(source: tuple[object, ...], address: str) -> bool:
    if len(source) < 2 or source[1] != 53 or type(source[0]) is not str:
        return False
    try:
        return ipaddress.ip_address(source[0]) == ipaddress.ip_address(address)
    except ValueError:
        return False


def resolve_txt(name: str, *, timeout: float = 3.0, server: str | None = None) -> DnsAnswer:
    """Look up the TXT records for `name`.

    Never raises: a failure to reach a nameserver, a malformed response, or a
    missing `/etc/resolv.conf` all come back as `DnsAnswer.error`. The caller is
    a diagnostic, and "I could not tell" is a different report from "it is not
    configured".
    """
    answer_name = name if type(name) is str else repr(name)
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _MAX_TIMEOUT
    ):
        return DnsAnswer(
            answer_name,
            error=f"timeout must be a finite number between 0 and 30 seconds, got {timeout!r}",
        )
    if server is not None:
        try:
            _nameserver_family(server)
        except ValueError as exc:
            return DnsAnswer(answer_name, error=str(exc))
        servers = [server]
    else:
        servers = _nameservers()[:_MAX_NAMESERVERS]
    if not servers:
        return DnsAnswer(answer_name, error="no nameserver configured (set WREATH_DNS_SERVER)")
    try:
        question = _encode_name(name)
    except ValueError as exc:
        return DnsAnswer(answer_name, error=str(exc))
    # `secrets` rather than `random` for the query id. It is not load-bearing --
    # the answer prints a configuration finding and never decides anything, so
    # guessing it buys an off-path attacker a misleading diagnostic at most --
    # but a predictable transaction id in a DNS client is the kind of thing that
    # gets reused somewhere it does matter, and the cost of getting it right
    # here is one import.
    query_id = secrets.randbelow(1 << 16)
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    packet = header + question + struct.pack("!HH", _TYPE_TXT, _CLASS_IN)

    last_error = "no nameserver answered"
    for address in servers:
        try:
            family = _nameserver_family(address)
            with socket.socket(family, socket.SOCK_DGRAM) as udp:
                udp.settimeout(timeout)
                udp.sendto(packet, (address, 53))
                response, source = udp.recvfrom(4096)
            if not _same_endpoint(source, address):
                raise ValueError(f"DNS answer came from unexpected endpoint {source!r}")
            if len(response) >= 3 and response[2] & 0x02:  # TC: truncated
                response = _over_tcp(packet, address, timeout)
            return DnsAnswer(answer_name, _parse(response, query_id, question))
        except (OSError, TypeError, ValueError, struct.error) as exc:
            last_error = f"{address}: {exc}"
    return DnsAnswer(answer_name, error=last_error)


def _over_tcp(packet: bytes, address: str, timeout: float) -> bytes:
    """Re-ask over TCP, which is what a truncated UDP answer means."""
    with socket.create_connection((address, 53), timeout=timeout) as tcp:
        tcp.sendall(struct.pack("!H", len(packet)) + packet)
        prefix = _recv_exactly(tcp, 2)
        (length,) = struct.unpack("!H", prefix)
        if length > _MAX_RESPONSE:
            raise ValueError("DNS response over TCP is implausibly large")
        return _recv_exactly(tcp, length)


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ValueError("DNS connection closed mid-response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
