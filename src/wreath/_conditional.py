"""Conditional-request comparison, in one place.

`If-None-Match` was parsed twice -- once in `wreath.staticfiles` for a file's
`ETag` and once in `wreath._auth.permissions` for the permission manifest's --
from the same clause of RFC 9110, with the same three cases and two spellings of
the loop. Two copies of a specification parser is how they drift, and the
direction they drift in is *serve a stale body* or *refetch every time*: both
call sites recorded, in their own docstrings, that they had already shipped the
naive `==` comparison and paid for it.

Minting an entity tag stays with whatever produced the representation -- bytes,
a `stat`, a policy fingerprint, a content coding -- because each knows something
the others do not. Only the *comparison* is one function, and this is it.

The statuses a response may not carry a body under are here for the same
reason. `frozenset({204, 304})` was written in four modules; it is one fact
about HTTP.
"""

from __future__ import annotations

from typing import Final

#: Statuses whose responses carry no body, whatever the handler returned.
#: RFC 9110 §15.3.5 (204) and §15.4.5 (304). The one-shot and streaming response paths
#: both consult this, as does the porting analyzer's rule for a handler that
#: returns one anyway.
STATUS_WITHOUT_BODY: Final[frozenset[int]] = frozenset({204, 304})


def etag_matches(header: str | None, tag: str) -> bool:
    """Whether an `If-None-Match` header covers `tag` (RFC 9110 §13.1.2).

    The header is a *list*, may be `*`, and its entries may carry the weak `W/`
    prefix -- which the weak comparison a conditional GET needs ignores.
    Comparing the whole header to one tag with `==` meant a client that sent two
    tags, or a proxy that added one, re-downloaded the body every time.

    Args:
        header: the raw `If-None-Match` value, or None when absent.
        tag: the entity tag this representation would send, unquoted or quoted
            exactly as it appears in the response.

    Returns:
        True when the client already holds this representation, so the caller
        should answer 304.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    target = tag.removeprefix("W/")
    return any(
        candidate.strip().removeprefix("W/") == target for candidate in header.split(",")
    )


__all__ = ["STATUS_WITHOUT_BODY", "etag_matches"]
