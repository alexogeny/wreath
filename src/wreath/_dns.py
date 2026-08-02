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

import os
import re
import secrets
import socket
import struct
from dataclasses import dataclass

__all__ = ["DnsAnswer", "resolve_txt"]

_TYPE_TXT = 16
_CLASS_IN = 1
#: RFC 1035 §2.3.4: 255 octets for a name, 63 per label.
_MAX_NAME = 255
_MAX_LABEL = 63
#: Enough for any TXT answer over TCP; a larger one is a server we do not want
#: to read anyway.
_MAX_RESPONSE = 65535


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
    if len(name) > _MAX_NAME:
        raise ValueError(f"a DNS name is at most {_MAX_NAME} octets")
    out = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("idna") if not label.isascii() else label.encode("ascii")
        if not encoded or len(encoded) > _MAX_LABEL:
            raise ValueError(f"invalid DNS label {label!r}")
        out.append(len(encoded))
        out += encoded
    out.append(0)
    return bytes(out)


def _skip_name(data: bytes, offset: int) -> int:
    """Advance past a (possibly compressed) name, returning the next offset."""
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            # A compression pointer is always the last thing in a name, and is
            # two octets. We never follow it: nothing here needs the name back,
            # only the position after it.
            return offset + 2
        offset += 1 + length


def _parse(response: bytes, query_id: int) -> tuple[str, ...]:
    if len(response) < 12:
        raise ValueError("DNS response shorter than a header")
    ident, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", response[:12])
    if ident != query_id:
        raise ValueError("DNS response id does not match the query")
    rcode = flags & 0xF
    if rcode == 3:
        return ()  # NXDOMAIN: a definite "there is no such name".
    if rcode != 0:
        raise ValueError(f"DNS server returned rcode {rcode}")
    offset = 12
    for _ in range(questions):
        offset = _skip_name(response, offset) + 4
    found: list[str] = []
    for _ in range(answers):
        offset = _skip_name(response, offset)
        if offset + 10 > len(response):
            raise ValueError("truncated DNS answer")
        rtype, _, _, rdlength = struct.unpack("!HHIH", response[offset : offset + 10])
        offset += 10
        rdata = response[offset : offset + rdlength]
        if len(rdata) != rdlength:
            raise ValueError("truncated DNS rdata")
        offset += rdlength
        if rtype != _TYPE_TXT:
            continue
        # A TXT record is a sequence of length-prefixed character-strings, each
        # at most 255 octets. A 2048-bit DKIM key arrives as several and means
        # nothing until they are concatenated -- splitting on the wrong boundary
        # is why a published key sometimes reads as malformed.
        parts: list[str] = []
        cursor = 0
        while cursor < len(rdata):
            size = rdata[cursor]
            parts.append(rdata[cursor + 1 : cursor + 1 + size].decode("utf-8", "replace"))
            cursor += 1 + size
        found.append("".join(parts))
    return tuple(found)


def resolve_txt(name: str, *, timeout: float = 3.0, server: str | None = None) -> DnsAnswer:
    """Look up the TXT records for `name`.

    Never raises: a failure to reach a nameserver, a malformed response, or a
    missing `/etc/resolv.conf` all come back as `DnsAnswer.error`. The caller is
    a diagnostic, and "I could not tell" is a different report from "it is not
    configured".
    """
    servers = [server] if server else _nameservers()
    if not servers:
        return DnsAnswer(name, error="no nameserver configured (set WREATH_DNS_SERVER)")
    try:
        question = _encode_name(name)
    except ValueError as exc:
        return DnsAnswer(name, error=str(exc))
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
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.settimeout(timeout)
                udp.sendto(packet, (address, 53))
                response, _ = udp.recvfrom(4096)
            if len(response) >= 3 and response[2] & 0x02:  # TC: truncated
                response = _over_tcp(packet, address, timeout)
            return DnsAnswer(name, _parse(response, query_id))
        except (OSError, ValueError, struct.error) as exc:
            last_error = f"{address}: {exc}"
    return DnsAnswer(name, error=last_error)


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
