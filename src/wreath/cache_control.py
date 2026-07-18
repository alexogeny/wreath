"""Typed HTTP cache-control policies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .request import Request


@dataclass(frozen=True, slots=True)
class CacheControl:
    _header: bytes = field(init=False, repr=False, compare=False)
    public: bool = False
    private: bool = False
    no_store: bool = False
    no_cache: bool = False
    no_transform: bool = False
    must_revalidate: bool = False
    proxy_revalidate: bool = False
    immutable: bool = False
    max_age: int | None = None
    shared_max_age: int | None = None
    stale_while_revalidate: int | None = None
    stale_if_error: int | None = None

    def __post_init__(self) -> None:
        if self.public and self.private:
            raise ValueError("cache policy cannot be both public and private")
        for name in (
            "max_age",
            "shared_max_age",
            "stale_while_revalidate",
            "stale_if_error",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.immutable and (self.max_age is None or self.max_age <= 0):
            raise ValueError("immutable cache policy requires a positive max_age")
        object.__setattr__(self, "_header", self._render_header())

    def _render_header(self) -> bytes:
        directives: list[str] = []
        for enabled, name in (
            (self.public, "public"),
            (self.private, "private"),
            (self.no_store, "no-store"),
            (self.no_cache, "no-cache"),
            (self.no_transform, "no-transform"),
            (self.must_revalidate, "must-revalidate"),
            (self.proxy_revalidate, "proxy-revalidate"),
            (self.immutable, "immutable"),
        ):
            if enabled:
                directives.append(name)
        for value, name in (
            (self.max_age, "max-age"),
            (self.shared_max_age, "s-maxage"),
            (self.stale_while_revalidate, "stale-while-revalidate"),
            (self.stale_if_error, "stale-if-error"),
        ):
            if value is not None:
                directives.append(f"{name}={value}")
        return ", ".join(directives).encode("ascii")

    def to_header(self) -> bytes:
        return self._header


type CachePolicy = Callable[["Request", Any], CacheControl | None]

PRIVATE_NO_STORE = CacheControl(private=True, no_store=True)

__all__ = ["CacheControl", "CachePolicy", "PRIVATE_NO_STORE"]
