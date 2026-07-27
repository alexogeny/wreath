"""Pure-Python Cedar program evaluator; the reference twin of _core.cedar_is_authorized.

The compiled program format is produced by :mod:`wreath._auth.cedar_engine`
and documented there: expressions are nested tuples headed by an integer
opcode; values are ``bool``, i64 ``int``, ``str``, ``(type, id)`` uid tuples,
duplicate-free ``list`` sets, and ``dict`` records. This module and
``wreath/_native/cedar.c`` must agree observably — the differential tests in
``tests/test_cedar_engine.py`` hold them to it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["cedar_is_authorized"]

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
_MAX_DEPTH = 200


class _EvalError(Exception):
    """An evaluation error, scoped to the one policy being evaluated."""


def _type_name(value: Any) -> str:
    if type(value) is bool:
        return "bool"
    if isinstance(value, int):
        return "long"
    if isinstance(value, str):
        return "string"
    if isinstance(value, tuple):
        return "entity"
    if isinstance(value, list):
        return "set"
    if isinstance(value, dict):
        return "record"
    return type(value).__name__


def _dedupe_key(value: Any) -> tuple[str, object] | None:
    """A hashable identity for a Cedar value, or None if it needs ``_cedar_eq``.

    The tag keeps kinds apart because :func:`_cedar_eq` does: ``True`` and ``1``
    are not equal in Cedar's model, but Python compares them equal and hashes
    them alike. Values of different kinds are never ``_cedar_eq``, so
    partitioning by kind loses nothing. Mirrors ``cedar_dedupe_key`` in
    ``_native/cedar.c`` and ``_dedupe_key`` in ``_auth/cedar_engine.py``.
    """
    if type(value) is bool:
        return ("b", value)
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, tuple):
        try:
            hash(value)
        except TypeError:
            return None
        return ("t", value)
    return None  # records and nested sets: structural comparison only


def _cedar_eq(a: Any, b: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        raise _EvalError("value is nested too deeply")
    if type(a) is bool or type(b) is bool:
        return type(a) is bool and type(b) is bool and a is b
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, tuple) and isinstance(b, tuple):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return all(any(_cedar_eq(x, y, depth + 1) for y in b) for x in a) and all(
            any(_cedar_eq(y, x, depth + 1) for x in a) for y in b
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(
            _cedar_eq(value, b[key], depth + 1) for key, value in a.items()
        )
    return False


def _like(value: str, segments: Any) -> bool:
    if len(segments) == 1:
        return value == segments[0]
    head, *middle, tail = segments
    if not value.startswith(head) or not value.endswith(tail):
        return False
    position = len(head)
    limit = len(value) - len(tail)
    if position > limit:
        return False
    for segment in middle:
        if not segment:
            continue
        found = value.find(segment, position, limit)
        if found < 0:
            return False
        position = found + len(segment)
    return True


def _ancestors(store: dict, uid: Any) -> frozenset:
    entry = store.get(uid)
    return entry[1] if entry is not None else frozenset()


def _evaluate(node: Any, request: tuple, store: dict, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise _EvalError("expression is nested too deeply")
    if not isinstance(node, tuple):  # pragma: no cover - the parser only emits tuples
        raise _EvalError("malformed program node")
    op = node[0]
    if op == 0:  # CONST
        return node[1]
    if op == 1:  # VAR
        return request[node[1]]
    if op == 2:  # AND
        left = _boolean(node[1], request, store, depth, "&&")
        if not left:
            return False
        return _boolean(node[2], request, store, depth, "&&")
    if op == 3:  # OR
        left = _boolean(node[1], request, store, depth, "||")
        if left:
            return True
        return _boolean(node[2], request, store, depth, "||")
    if op == 4:  # NOT
        return not _boolean(node[1], request, store, depth, "!")
    if op == 5:  # ARITH
        left = _integer(node[2], request, store, depth, "arithmetic")
        right = _integer(node[3], request, store, depth, "arithmetic")
        kind = node[1]
        if kind == 0:
            result = left + right
        elif kind == 1:
            result = left - right
        else:
            result = left * right
        if not _I64_MIN <= result <= _I64_MAX:
            raise _EvalError("arithmetic overflowed i64")
        return result
    if op == 6:  # CMP
        left = _integer(node[2], request, store, depth, "comparison")
        right = _integer(node[3], request, store, depth, "comparison")
        kind = node[1]
        if kind == 0:
            return left < right
        if kind == 1:
            return left <= right
        if kind == 2:
            return left > right
        return left >= right
    if op == 7:  # EQ
        return _cedar_eq(
            _evaluate(node[1], request, store, depth + 1),
            _evaluate(node[2], request, store, depth + 1),
        )
    if op == 8:  # NE
        return not _cedar_eq(
            _evaluate(node[1], request, store, depth + 1),
            _evaluate(node[2], request, store, depth + 1),
        )
    if op == 9:  # IN
        left = _evaluate(node[1], request, store, depth + 1)
        if not _is_uid(left):
            raise _EvalError(f"'in' requires an entity, got {_type_name(left)}")
        right = _evaluate(node[2], request, store, depth + 1)
        if _is_uid(right):
            candidates = (right,)
        elif isinstance(right, list):
            candidates = tuple(right)
        else:
            raise _EvalError(
                f"'in' requires an entity or a set of entities, got {_type_name(right)}"
            )
        ancestors = _ancestors(store, left)
        for candidate in candidates:
            if not _is_uid(candidate):
                raise _EvalError(
                    f"'in' requires entities on the right, got {_type_name(candidate)}"
                )
            if candidate == left or candidate in ancestors:
                return True
        return False
    if op == 10:  # HAS
        operand = _evaluate(node[1], request, store, depth + 1)
        if _is_uid(operand):
            entry = store.get(operand)
            return entry is not None and node[2] in entry[0]
        if isinstance(operand, dict):
            return node[2] in operand
        raise _EvalError(f"'has' requires an entity or record, got {_type_name(operand)}")
    if op == 11:  # LIKE
        operand = _evaluate(node[1], request, store, depth + 1)
        if not isinstance(operand, str):
            raise _EvalError(f"'like' requires a string, got {_type_name(operand)}")
        return _like(operand, node[2])
    if op == 12:  # IS
        operand = _evaluate(node[1], request, store, depth + 1)
        if not _is_uid(operand):
            raise _EvalError(f"'is' requires an entity, got {_type_name(operand)}")
        if operand[0] != node[2]:
            return False
        if node[3] is None:
            return True
        return _evaluate((9, (0, operand), node[3]), request, store, depth + 1)
    if op == 13:  # IF
        if _boolean(node[1], request, store, depth, "if"):
            return _evaluate(node[2], request, store, depth + 1)
        return _evaluate(node[3], request, store, depth + 1)
    if op == 14:  # SET
        # Deduplicated by hash where the member allows it. Comparing each
        # candidate against every kept one is O(n**2), and a set literal is
        # re-evaluated on every authorization, so a policy holding a few
        # hundred entries (an allowlist, a tenant list) paid that per request.
        # `_native/cedar.c` does the same thing the same way; the two must stay
        # algorithmically aligned as well as byte-for-byte.
        items: list[object] = []
        seen: set[tuple[str, object]] = set()
        structural: list[object] = []
        for item_node in node[1]:
            candidate = _evaluate(item_node, request, store, depth + 1)
            key = _dedupe_key(candidate)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            else:
                if any(_cedar_eq(candidate, existing) for existing in structural):
                    continue
                structural.append(candidate)
            items.append(candidate)
        return items
    if op == 15:  # RECORD
        return {key: _evaluate(value, request, store, depth + 1) for key, value in node[1]}
    if op == 16:  # GETATTR
        operand = _evaluate(node[1], request, store, depth + 1)
        attribute = node[2]
        if _is_uid(operand):
            entry = store.get(operand)
            if entry is None:
                raise _EvalError(f"entity {operand!r} has no attributes in this request")
            if attribute not in entry[0]:
                raise _EvalError(f"entity {operand!r} has no attribute {attribute!r}")
            return entry[0][attribute]
        if isinstance(operand, dict):
            if attribute not in operand:
                raise _EvalError(f"record has no attribute {attribute!r}")
            return operand[attribute]
        raise _EvalError(
            f"attribute access requires an entity or record, got {_type_name(operand)}"
        )
    if op == 17:  # METHOD
        operand = _evaluate(node[2], request, store, depth + 1)
        if not isinstance(operand, list):
            raise _EvalError(f"set methods require a set, got {_type_name(operand)}")
        method = node[1]
        if method == 3:  # isEmpty
            return not operand
        argument = _evaluate(node[3], request, store, depth + 1)
        if method == 0:  # contains
            return any(_cedar_eq(argument, item) for item in operand)
        if not isinstance(argument, list):
            raise _EvalError("containsAll/containsAny require a set argument")
        if method == 1:  # containsAll
            return all(any(_cedar_eq(x, y) for y in operand) for x in argument)
        return any(any(_cedar_eq(x, y) for y in operand) for x in argument)
    raise _EvalError(f"unknown opcode {op!r}")  # pragma: no cover - parser-controlled


def _is_uid(value: Any) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], str)
    )


def _boolean(node: Any, request: tuple, store: dict, depth: int, context: str) -> bool:
    value = _evaluate(node, request, store, depth + 1)
    if type(value) is not bool:
        raise _EvalError(f"{context} requires a bool, got {_type_name(value)}")
    return value


def _integer(node: Any, request: tuple, store: dict, depth: int, context: str) -> int:
    value = _evaluate(node, request, store, depth + 1)
    if type(value) is bool or not isinstance(value, int):
        raise _EvalError(f"{context} requires a long, got {_type_name(value)}")
    return value


def _scope_matches(scope: Any, uid: Any, store: dict) -> bool:
    kind = scope[0]
    if kind == 0:  # any
        return True
    if kind == 1:  # ==
        return uid == scope[1]
    if kind == 2:  # in
        return uid == scope[1] or scope[1] in _ancestors(store, uid)
    if kind == 3:  # in [..]
        if uid in scope[1]:
            return True
        ancestors = _ancestors(store, uid)
        return any(candidate in ancestors for candidate in scope[1])
    if uid[0] != scope[1]:  # is
        return False
    if scope[2] is None:
        return True
    return uid == scope[2] or scope[2] in _ancestors(store, uid)


def cedar_is_authorized(
    policies: tuple,
    principal: tuple[str, str],
    action: tuple[str, str],
    resource: tuple[str, str],
    context: dict,
    store: dict,
) -> tuple[bool, str, tuple[str, ...]]:
    """Evaluate a compiled policy set; forbid overrides permit, default deny.

    Returns ``(allowed, reason, diagnostics)``. A policy that raises an
    evaluation error is skipped and reported in the diagnostics — it never
    silently satisfies, and it never turns into a deny by itself.
    """
    request = (principal, action, resource, context)
    permitted = False
    forbidden = False
    diagnostics: list[str] = []
    for policy in policies:
        forbid, policy_id, principal_scope, action_scope, resource_scope, conditions = policy
        try:
            if not (
                _scope_matches(principal_scope, principal, store)
                and _scope_matches(action_scope, action, store)
                and _scope_matches(resource_scope, resource, store)
            ):
                continue
            satisfied = True
            for unless, expression in conditions:
                value = _evaluate(expression, request, store, 0)
                if type(value) is not bool:
                    raise _EvalError(
                        f"condition evaluated to {_type_name(value)}, not bool"
                    )
                if bool(unless) == value:
                    satisfied = False
                    break
            if not satisfied:
                continue
            effect = "forbid" if forbid else "permit"
            diagnostics.append(f"{effect} {policy_id} matched")
            if forbid:
                forbidden = True
            else:
                permitted = True
        except _EvalError as error:
            effect = "forbid" if forbid else "permit"
            diagnostics.append(f"{effect} {policy_id} skipped: {error}")
    if forbidden:
        return (False, "explicit forbid", tuple(diagnostics))
    if permitted:
        return (True, "cedar permit", tuple(diagnostics))
    return (False, "no permit policy matched", tuple(diagnostics))
