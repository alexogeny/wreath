from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime

from ._structured_fields import Date, Item, serialize_item


def _instant(name: str, value: datetime | None) -> tuple[int, datetime] | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a timezone-aware datetime or None")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; pass tzinfo=UTC")
    utc = value.astimezone(UTC)
    return int(utc.timestamp()), utc


def lifecycle_headers(
    *,
    deprecated_at: datetime | None,
    sunset_at: datetime | None,
    deprecation_link: str | None,
) -> tuple[tuple[bytes, bytes], ...]:
    deprecated = _instant("deprecated_at", deprecated_at)
    sunset = _instant("sunset_at", sunset_at)
    if deprecated is not None and sunset is not None and sunset[0] < deprecated[0]:
        raise ValueError("sunset_at must not be earlier than deprecated_at")

    headers: list[tuple[bytes, bytes]] = []
    if deprecated is not None:
        headers.append((b"deprecation", serialize_item(Item(Date(deprecated[0])))))
    if sunset is not None:
        headers.append(
            (
                b"sunset",
                format_datetime(sunset[1], usegmt=True).encode("ascii"),
            )
        )
    if deprecation_link is not None:
        if not isinstance(deprecation_link, str):
            raise TypeError("deprecation_link must be a URI-reference string or None")
        if not deprecation_link or any(
            character.isspace() or character in '<>"' for character in deprecation_link
        ):
            raise ValueError(
                "deprecation_link must be a URI-reference without whitespace, '<', '>', or '\"'"
            )
        try:
            link_value = f'<{deprecation_link}>; rel="deprecation"'.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError(
                "deprecation_link must be an ASCII URI-reference; percent-encode "
                "non-ASCII characters"
            ) from error
        headers.append(
            (
                b"link",
                link_value,
            )
        )
    return tuple(headers)
