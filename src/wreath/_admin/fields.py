"""Per-*field* authorization: which columns this request may read and write.

Cedar decides per action. An admin needs per field -- a support agent sees the
email address, a contractor sees it redacted -- and that is the one thing a
third-party admin tool cannot do, because it does not hold the policy set.

No new decision point is invented for it. A protected field names an ordinary
Cedar action, which is asked exactly the way `PrecisionLadder` asks its rungs:
once per request, cached on `request.state`, fail-closed when no answer can be
obtained. A list page asks the same question for every row and the answer cannot
differ between rows of one response, so resolving per row would multiply
`rows x fields` authorization calls to reach a value that was already known.

The resolved set is applied at the **data** boundary rather than in the
template. A withheld column never enters the render context at all, so there is
no second projection to forget and no template branch that could publish it by
being edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .._auth.requirements import PolicyRequirement

__all__ = ["FieldAccess", "resolve_readable", "resolve_writable"]


@dataclass(frozen=True, slots=True)
class FieldAccess:
    """The Cedar actions that permit reading and writing one column.

    ```python
    admin.register(User, field_access={
        "email": FieldAccess(read="read_contact"),
        "plan":  FieldAccess(read="read_billing", write="change_billing"),
    })
    ```

    `read=None` means the column inherits the model's own read rule -- it is
    visible to anyone the list and detail views already admit. `write=None`
    likewise inherits, so a column may be freely readable and separately gated
    on write, or the reverse.

    A column that is unreadable is also unwritable, whatever `write` says. The
    alternative is a form that accepts a value for a field its author cannot
    see, which is an overwrite nobody can review.
    """

    read: str | None = None
    write: str | None = None


def _slot(admin_id: int, mode: str, columns: tuple[str, ...]) -> str:
    """The per-request cache key for one resolution.

    `columns` is part of the key, not just `(admin, mode)`. The detail view asks
    about every shown column and the form asks about the editable ones, so two
    resolutions with different column tuples exist in the same mode -- and
    sharing a slot between them would let whichever ran first answer for the
    other, silently returning a set computed over the wrong names.
    """
    return f"_admin_fields_{admin_id:x}_{mode}_{len(columns)}_{hash(columns) & 0xFFFFFFFF:x}"


async def _resolve(
    request: Any,
    authorizer: Any,
    field_access: Mapping[str, FieldAccess],
    columns: tuple[str, ...],
    resource: object,
    mode: str,
    admin_id: int,
) -> frozenset[str]:
    """The subset of `columns` this request may `mode` (read/write).

    Cached per request per mode. A column with no rule for this mode is
    permitted -- it is governed by the route's own `Access` rule, which already
    ran. A column *with* a rule is permitted only on an explicit Cedar allow.
    """
    state = request.state
    slot = _slot(admin_id, mode, columns)
    cached = state.get(slot)
    if cached is not None:
        return cached[0]
    # The names that carry a rule for this mode. `withheld` below is what the
    # generated views project against, and it is deliberately one comprehension
    # rather than a branch per column: a filter that is one expression is a
    # control `wreath mutant` can remove whole, and a control it can remove is
    # a control a test can be made to notice.
    gated = {
        name: getattr(field_access[name], mode)
        for name in columns
        if name in field_access and getattr(field_access[name], mode) is not None
    }
    if not gated:
        permitted = frozenset(columns)
    elif authorizer is None:
        # A declared field rule and no authorizer is a decision nobody can
        # make, and publishing would answer it "yes". Withhold every gated
        # column; the ungated ones are unaffected because no policy was ever
        # claimed for them.
        permitted = frozenset(name for name in columns if name not in gated)
    else:
        allowed: set[str] = set()
        # One authorization call per distinct action, not per column: two
        # columns behind `read_contact` are one question.
        verdicts: dict[str, bool] = {}
        for name, action in gated.items():
            verdict = verdicts.get(action)
            if verdict is None:
                decision = await authorizer.authorize(
                    request, PolicyRequirement(action=action, resource=resource)
                )
                verdict = bool(getattr(decision, "allowed", False))
                verdicts[action] = verdict
            if verdict:
                allowed.add(name)
        permitted = frozenset(
            name for name in columns if name not in gated or name in allowed
        )
    # Boxed, because the empty frozenset is a legitimate answer and `state.get`
    # cannot tell it from a miss.
    state.__setattr__(slot, (permitted,))
    return permitted


async def resolve_readable(
    request: Any,
    authorizer: Any,
    field_access: Mapping[str, FieldAccess],
    columns: tuple[str, ...],
    resource: object,
    admin_id: int,
) -> frozenset[str]:
    """Columns of `columns` this request may read, in declaration order."""
    return await _resolve(
        request, authorizer, field_access, columns, resource, "read", admin_id
    )


async def resolve_writable(
    request: Any,
    authorizer: Any,
    field_access: Mapping[str, FieldAccess],
    columns: tuple[str, ...],
    resource: object,
    admin_id: int,
) -> frozenset[str]:
    """Columns of `columns` this request may write.

    Intersected with the readable set: a field its author cannot see is a field
    they cannot knowingly change, and accepting a value for it would be an
    overwrite nobody can review.
    """
    writable = await _resolve(
        request, authorizer, field_access, columns, resource, "write", admin_id
    )
    readable = await resolve_readable(
        request, authorizer, field_access, columns, resource, admin_id
    )
    return writable & readable
