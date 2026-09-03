from __future__ import annotations

import struct

import pytest

from wreath._dkim import Ed25519Key, RsaKey
from wreath._dns import DnsAnswer, resolve_txt
from wreath._userkit import SmtpEmailSender
from wreath.doctor import check_email_deliverability

RSA = RsaKey(n=(1 << 2047) | 1, e=65537, d=3)
ED = Ed25519Key(bytes(32))


def stub(records: dict[str, list[str]], *, unreachable: bool = False):
    """A resolver over a fixed table. Absent names resolve to nothing."""

    def resolve(name: str, *, timeout: float = 3.0, **_: object) -> DnsAnswer:
        if unreachable:
            return DnsAnswer(name, error="connection timed out")
        return DnsAnswer(name, tuple(records.get(name, ())))

    return resolve


def signer(domain: str = "example.com", selector: str = "sel", key: object = RSA):
    from wreath._dkim import DkimSigner

    return DkimSigner(domain, selector, key)


def sender(*, from_addr: str = "mail@example.com", **kwargs: object) -> SmtpEmailSender:
    return SmtpEmailSender(host="localhost", from_addr=from_addr, **kwargs)


HEALTHY = {
    "example.com": ["v=spf1 include:_spf.example.net -all"],
    "sel._domainkey.example.com": ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
    "_dmarc.example.com": ["v=DMARC1; p=quarantine; rua=mailto:d@example.com"],
}


def test_a_fully_configured_domain_reports_nothing() -> None:
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(HEALTHY))
    assert findings == []


def test_an_unsigned_sender_is_the_first_finding() -> None:
    findings = check_email_deliverability(sender(), resolve=stub(HEALTHY))
    assert "sends unsigned mail" in findings[0]


def test_a_missing_dkim_record_names_the_record_to_publish() -> None:
    records = dict(HEALTHY)
    del records["sel._domainkey.example.com"]
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("sel._domainkey.example.com" in line for line in findings)


def test_a_revoked_dkim_key_is_distinguished_from_a_missing_one() -> None:
    records = dict(HEALTHY) | {"sel._domainkey.example.com": ["v=DKIM1; k=rsa; p="]}
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("revoked" in line for line in findings)


def test_a_versioned_dkim_record_without_a_key_is_revoked_not_missing() -> None:
    records = dict(HEALTHY) | {
        "sel._domainkey.example.com": ["v=DKIM1; k=rsa"],
    }
    findings = check_email_deliverability(
        sender(dkim=signer()),
        resolve=stub(records),
    )

    assert any("revoked" in line for line in findings)
    assert not any("no DKIM public key" in line for line in findings)


def test_a_misaligned_signing_domain_is_reported_even_when_dns_is_perfect() -> None:
    records = dict(HEALTHY) | {"sel._domainkey.other.example": ["v=DKIM1; k=rsa; p=MIIBIjANBg"]}
    findings = check_email_deliverability(
        sender(dkim=signer(domain="other.example")), resolve=stub(records)
    )
    assert any("align" in line for line in findings)


def test_an_ed25519_signer_needs_the_key_type_published() -> None:
    records = dict(HEALTHY)
    findings = check_email_deliverability(sender(dkim=signer(key=ED)), resolve=stub(records))
    assert any("k=ed25519" in line for line in findings)


def test_a_missing_spf_record_is_reported() -> None:
    records = {k: v for k, v in HEALTHY.items() if k != "example.com"}
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("no SPF record" in line for line in findings)


def test_two_spf_records_are_reported_as_a_permerror() -> None:
    records = dict(HEALTHY) | {"example.com": ["v=spf1 include:a -all", "v=spf1 include:b -all"]}
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("permerror" in line for line in findings)


def test_a_permissive_spf_record_is_reported() -> None:
    records = dict(HEALTHY) | {"example.com": ["v=spf1 +all"]}
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("+all" in line for line in findings)


def test_a_missing_dmarc_record_is_reported() -> None:
    records = {k: v for k, v in HEALTHY.items() if k != "_dmarc.example.com"}
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("no DMARC record" in line for line in findings)


def test_p_none_is_reported_as_below_the_baseline() -> None:
    records = dict(HEALTHY) | {"_dmarc.example.com": ["v=DMARC1; p=none"]}
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub(records))
    assert any("p=none" in line for line in findings)


def test_an_unreachable_resolver_says_so_instead_of_reporting_misconfiguration() -> None:
    findings = check_email_deliverability(sender(dkim=signer()), resolve=stub({}, unreachable=True))
    assert findings
    assert all("could not read" in line for line in findings)


def test_a_sender_with_no_from_address_is_reported_once() -> None:
    findings = check_email_deliverability(sender(from_addr=""), resolve=stub(HEALTHY))
    assert len(findings) == 1
    assert "no from address" in findings[0]


def _response(query_id: int, *records: bytes, rcode: int = 0, answers: int | None = None) -> bytes:
    """Build a wire-format DNS answer for `example.com IN TXT`."""
    header = struct.pack(
        "!HHHHHH", query_id, 0x8180 | rcode, 1, len(records) if answers is None else answers, 0, 0
    )
    question = b"\x07example\x03com\x00" + struct.pack("!HH", 16, 1)
    body = b""
    for record in records:
        # 0xC00C is a compression pointer back to the question's name.
        body += b"\xc0\x0c" + struct.pack("!HHIH", 16, 1, 300, len(record)) + record
    return header + question + body


def _txt(*strings: bytes) -> bytes:
    return b"".join(bytes([len(s)]) + s for s in strings)


def test_a_multi_string_txt_record_is_joined() -> None:
    from wreath._dns import _parse

    parsed = _parse(_response(7, _txt(b"v=DKIM1; k=rsa; p=AAAA", b"BBBBCCCC")), 7)
    assert parsed == ("v=DKIM1; k=rsa; p=AAAABBBBCCCC",)


def test_an_nxdomain_is_an_empty_answer_not_an_error() -> None:
    from wreath._dns import _parse

    assert _parse(_response(7, rcode=3), 7) == ()


def test_a_mismatched_response_id_is_refused() -> None:
    from wreath._dns import _parse

    with pytest.raises(ValueError, match="does not match"):
        _parse(_response(7, _txt(b"v=spf1")), 9)


def test_a_server_failure_is_an_error_not_an_empty_answer() -> None:
    from wreath._dns import _parse

    with pytest.raises(ValueError, match="rcode 2"):
        _parse(_response(7, rcode=2), 7)


def test_a_truncated_answer_is_refused_rather_than_half_parsed() -> None:
    from wreath._dns import _parse

    with pytest.raises(ValueError):
        _parse(_response(7, _txt(b"v=spf1"))[:-4], 7)


def test_a_lookup_with_no_nameserver_reports_that_it_could_not_tell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wreath._dns._nameservers", lambda: [])
    answer = resolve_txt("example.com")
    assert not answer.resolved
    assert answer.error == "no nameserver configured (set WREATH_DNS_SERVER)"


def test_an_over_long_name_is_refused_without_a_socket() -> None:
    answer = resolve_txt("a" * 300 + ".example.com", server="203.0.113.1")
    assert not answer.resolved
    assert "at most 255" in (answer.error or "")


def test_a_truncated_udp_answer_is_retried_over_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import struct

    from wreath import _dns

    full = _response(0, _txt(b"v=DKIM1; k=rsa; p=" + b"A" * 200))
    asked_over_tcp: list[bytes] = []

    class FakeUdp:
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def __enter__(self) -> FakeUdp:
            return self

        def __exit__(self, *exc: object) -> None: ...
        def settimeout(self, timeout: float) -> None: ...
        def sendto(self, packet: bytes, address: tuple[str, int]) -> None:
            self._id = struct.unpack("!H", packet[:2])[0]

        def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
            # Header with TC set, and a deliberately useless body: a resolver
            # that returns this instead of re-asking has silently lost the key.
            header = struct.pack("!HHHHHH", self._id, 0x8380, 1, 0, 0, 0)
            return header + b"\x07example\x03com\x00" + struct.pack("!HH", 16, 1), ("", 53)

    def fake_tcp(packet: bytes, address: str, timeout: float) -> bytes:
        asked_over_tcp.append(packet)
        return full[:2].replace(full[:2], packet[:2]) + full[2:]

    monkeypatch.setattr(socket, "socket", FakeUdp)
    monkeypatch.setattr(
        _dns,
        "_nameservers",
        lambda: pytest.fail("an explicit server must bypass resolver discovery"),
    )
    monkeypatch.setattr(_dns, "_over_tcp", fake_tcp)
    answer = _dns.resolve_txt("example.com", server="203.0.113.1")

    assert asked_over_tcp, "a truncated answer must be re-asked over TCP"
    assert answer.records == ("v=DKIM1; k=rsa; p=" + "A" * 200,)


def test_a_response_shorter_than_the_dns_header_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    class FakeUdp:
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def __enter__(self) -> FakeUdp:
            return self

        def __exit__(self, *exc: object) -> None: ...
        def settimeout(self, timeout: float) -> None: ...
        def sendto(self, packet: bytes, address: tuple[str, int]) -> None: ...
        def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
            return b"", ("", 53)

    monkeypatch.setattr(socket, "socket", FakeUdp)
    answer = resolve_txt("example.com", server="203.0.113.1")

    assert not answer.resolved
    assert "203.0.113.1" in (answer.error or "")


def test_an_untruncated_answer_is_not_re_asked_over_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import struct

    from wreath import _dns

    tcp_calls: list[bytes] = []

    class FakeUdp:
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def __enter__(self) -> FakeUdp:
            return self

        def __exit__(self, *exc: object) -> None: ...
        def settimeout(self, timeout: float) -> None: ...
        def sendto(self, packet: bytes, address: tuple[str, int]) -> None:
            self._id = struct.unpack("!H", packet[:2])[0]

        def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
            return _response(self._id, _txt(b"v=spf1 -all")), ("", 53)

    monkeypatch.setattr(socket, "socket", FakeUdp)
    monkeypatch.setattr(_dns, "_over_tcp", lambda p, a, t: tcp_calls.append(p) or b"")
    answer = _dns.resolve_txt("example.com", server="203.0.113.1")

    assert tcp_calls == []
    assert answer.records == ("v=spf1 -all",)
