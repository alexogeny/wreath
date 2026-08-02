"""Pure-Python trusted-proxy matcher twins.

Mirrors `wreath._native.proxy`. The C parser is deliberately stricter than
`ipaddress` in one respect -- it rejects zone identifiers ("fe80::1%eth0"),
which are meaningless in a forwarded hop -- so this twin rejects them too.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_ip(text: str) -> _Address | None:
    if "%" in text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _parse_cidr(text: str) -> _Network:
    if "%" in text:
        raise ValueError(f"invalid trusted proxy network: {text}")
    try:
        return ipaddress.ip_network(text, strict=True)
    except ValueError as error:
        raise ValueError(f"invalid trusted proxy network: {text}") from error


def _hop_address(hop: bytes) -> str | None:
    """Strip IPv6 brackets and an optional port from one forwarded hop."""
    try:
        text = hop.decode("ascii")
    except UnicodeDecodeError:
        return None
    if text.startswith("["):
        close = text.find("]")
        if close < 0:
            return None
        rest = text[close + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit() and len(rest) > 1):
            return None
        return text[1:close]
    # A bare IPv6 hop carries several colons and never a port, so only a lone
    # colon is a port separator.
    if text.count(":") == 1:
        return text.partition(":")[0]
    return text


class TrustedNetworks:
    """Compiled trusted-proxy network allow-list (immutable)."""

    __slots__ = ("_networks", "count")

    def __init__(self, networks: Iterable[str]) -> None:
        compiled: list[_Network] = []
        for item in networks:
            if not isinstance(item, str):
                raise TypeError("networks must be an iterable of strings")
            compiled.append(_parse_cidr(item))
        self._networks = tuple(compiled)
        self.count = len(compiled)

    def _contains(self, address: _Address) -> bool:
        return any(address in network for network in self._networks)

    def contains(self, address: str) -> bool:
        if not isinstance(address, str):
            raise TypeError("address must be a string")
        parsed = _parse_ip(address)
        return parsed is not None and self._contains(parsed)

    def forwarded_client(self, value: bytes) -> str | None:
        """Rightmost X-Forwarded-For hop that is not a trusted proxy.

        Returns None when any hop fails to parse: a malformed chain means the
        boundary between forged and vouched-for hops is unknowable.
        """
        data = bytes(value)
        last: _Address | None = None
        end = len(data)
        while True:
            start = data.rfind(b",", 0, end) + 1
            hop = data[start:end].strip(b" \t")
            text = _hop_address(hop)
            if text is None:
                return None
            address = _parse_ip(text)
            if address is None:
                return None
            if not self._contains(address):
                return str(address)
            last = address
            if start == 0:
                # Every hop was a trusted proxy: the leftmost is the client.
                return str(last)
            end = start - 1


__all__ = ["TrustedNetworks"]
