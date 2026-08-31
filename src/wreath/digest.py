"""RFC 9530 integrity fields and algorithm preference negotiation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import new as new_hash
from hmac import compare_digest

from ._structured_fields import (
    Item,
    StructuredFieldError,
    parse_dictionary,
    serialize_dictionary,
)

__all__ = ["Digest", "DigestError", "DigestPreferences"]

SUPPORTED_DIGEST_ALGORITHMS = ("sha-256", "sha-512")


class DigestError(ValueError):
    """An integrity field is malformed, unusable, or does not match its data."""


def _checksum(algorithm: str, content: bytes) -> bytes:
    return new_hash(algorithm.replace("-", ""), content).digest()


@dataclass(frozen=True, slots=True, init=False)
class Digest:
    """One parsed or computed Content-Digest/Repr-Digest field value."""

    _values: tuple[tuple[str, bytes], ...]
    header: bytes

    def __init__(self, values: Mapping[str, bytes]) -> None:
        items = tuple(values.items())
        if not items:
            raise DigestError("digest field needs at least one algorithm")
        members: dict[str, Item] = {}
        for algorithm, value in items:
            if not isinstance(value, bytes):
                raise DigestError(f"digest for {algorithm!r} must be a byte sequence")
            members[algorithm] = Item(value)
        try:
            header = serialize_dictionary(members)
        except (TypeError, ValueError) as error:
            raise DigestError(str(error)) from error
        object.__setattr__(self, "_values", items)
        object.__setattr__(self, "header", header)

    @classmethod
    def compute(cls, content: bytes, *algorithms: str) -> Digest:
        """Hash `content` with each named active algorithm and serialize the field."""
        if not isinstance(content, bytes):
            raise TypeError(f"digest content must be bytes, got {type(content).__name__}")
        if not algorithms:
            raise ValueError("digest computation needs at least one algorithm")
        if len(set(algorithms)) != len(algorithms):
            raise ValueError("digest algorithms must be unique")
        values: dict[str, bytes] = {}
        for algorithm in algorithms:
            if algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
                supported = ", ".join(SUPPORTED_DIGEST_ALGORITHMS)
                raise ValueError(f"unsupported digest algorithm {algorithm!r}; use {supported}")
            values[algorithm] = _checksum(algorithm, content)
        return cls(values)

    @classmethod
    def parse(cls, value: bytes | str) -> Digest:
        """Parse a bounded RFC 9530 integrity field."""
        try:
            members = parse_dictionary(value)
        except (StructuredFieldError, TypeError) as error:
            raise DigestError(str(error)) from error
        values: dict[str, bytes] = {}
        for algorithm, item in members.items():
            if item.parameters:
                raise DigestError(f"digest for {algorithm!r} must not have parameters")
            if not isinstance(item.value, bytes):
                raise DigestError(f"digest for {algorithm!r} must be a byte sequence")
            values[algorithm] = item.value
        return cls(values)

    @property
    def algorithms(self) -> tuple[str, ...]:
        """Algorithms carried by the field, in wire order."""
        return tuple(algorithm for algorithm, _value in self._values)

    def expectation(self) -> tuple[str, bytes]:
        """The strongest active algorithm this field carries."""
        values = dict(self._values)
        for algorithm in reversed(SUPPORTED_DIGEST_ALGORITHMS):
            value = values.get(algorithm)
            if value is not None:
                return algorithm, value
        raise DigestError("digest field names no supported algorithm")

    def verify(self, content: bytes) -> str:
        """Verify `content` against the strongest supported member and name it."""
        algorithm, expected = self.expectation()
        if not compare_digest(_checksum(algorithm, content), expected):
            raise DigestError(f"content does not match its {algorithm} digest")
        return algorithm


@dataclass(frozen=True, slots=True, init=False)
class DigestPreferences:
    """Want-Content-Digest or Want-Repr-Digest weighted preferences."""

    _weights: tuple[tuple[str, int], ...]
    header: bytes

    def __init__(self, weights: Mapping[str, int]) -> None:
        items = tuple(weights.items())
        if not items:
            raise DigestError("digest preferences need at least one algorithm")
        members: dict[str, Item] = {}
        for algorithm, weight in items:
            if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= 10:
                raise DigestError(
                    f"digest preference for {algorithm!r} must be an integer from 0 to 10"
                )
            members[algorithm] = Item(weight)
        try:
            header = serialize_dictionary(members)
        except (TypeError, ValueError) as error:
            raise DigestError(str(error)) from error
        object.__setattr__(self, "_weights", items)
        object.__setattr__(self, "header", header)

    @classmethod
    def parse(cls, value: bytes | str) -> DigestPreferences:
        """Parse a bounded RFC 9530 integrity preference field."""
        try:
            members = parse_dictionary(value)
        except (StructuredFieldError, TypeError) as error:
            raise DigestError(str(error)) from error
        weights: dict[str, int] = {}
        for algorithm, item in members.items():
            if item.parameters:
                raise DigestError(f"digest preference for {algorithm!r} must not have parameters")
            weight = item.value
            if isinstance(weight, bool) or not isinstance(weight, int):
                raise DigestError(
                    f"digest preference for {algorithm!r} must be an integer from 0 to 10"
                )
            weights[algorithm] = weight
        return cls(weights)

    def preferred(self, *supported: str) -> str | None:
        """Highest acceptable weight, breaking ties by server preference order."""
        weights = dict(self._weights)
        best: str | None = None
        best_weight = 0
        for algorithm in supported:
            weight = weights.get(algorithm, 0)
            if weight > best_weight:
                best = algorithm
                best_weight = weight
        return best
