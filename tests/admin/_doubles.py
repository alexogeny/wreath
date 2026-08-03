"""Shared doubles for the generated admin.

Deliberately the same shape as `tests/test_crud.py`'s: the admin is a client of
the ordinary stack, so a session double that satisfies generated CRUD should
satisfy this too, and any place it does not is a place the admin took a
privileged path.
"""

from __future__ import annotations

from typing import Any


class Null:
    async def __aenter__(self) -> Null:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    """Enough `Session` for the generated views, and nothing more."""

    def __init__(self, rows: dict[Any, Any] | None = None) -> None:
        self.rows = dict(rows or {})
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.closed = 0
        self._next = 100

    async def get(self, model: type, pk: Any) -> Any:
        return self.rows.get(pk)

    async def fetch(self, query: Any) -> list[Any]:
        return list(self.rows.values())

    async def count(self, query: Any) -> int:
        return len(self.rows)

    def add(self, instance: Any) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = self._next
            self._next += 1
        self.rows[instance.id] = instance
        self.added.append(instance)

    def delete(self, instance: Any) -> None:
        self.deleted.append(instance)
        self.rows.pop(instance.id, None)

    async def flush(self) -> None:
        return None

    def begin(self) -> Null:
        return Null()

    async def close(self) -> None:
        self.closed += 1


class State:
    """`request.state`: attribute storage with a `get`, as the real one has."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_values":
            super().__setattr__(name, value)
        else:
            self._values[name] = value


class Identity:
    def __init__(self, sub: str = "7") -> None:
        self.sub = sub


class App:
    def __init__(self, authorizer: Any = None) -> None:
        self._authorizer = authorizer


#: Distinguishes "the test did not care about identity" from "the test means
#: anonymous". Defaulting `identity=None` to a signed-in double would have made
#: the anonymous case unwritable, which is the case worth writing.
UNSET: Any = object()


class Request:
    """A request double carrying only what the generated views read."""

    def __init__(
        self,
        *,
        path_params: dict[str, str] | None = None,
        query: bytes = b"",
        form: dict[str, str] | None = None,
        identity: Any = UNSET,
        authorizer: Any = None,
    ) -> None:
        self.path_params = path_params or {}
        self.query_string = query
        self.state = State()
        self.identity = Identity() if identity is UNSET else identity
        self.app = App(authorizer)
        self._form = form or {}

    async def form(self) -> dict[str, str]:
        return self._form


class Decision:
    __slots__ = ("allowed", "reason")

    def __init__(self, allowed: bool, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason


class Authorizer:
    """A Cedar authorizer double: allow exactly the named actions, count calls."""

    def __init__(self, *allowed: str) -> None:
        self.allowed = frozenset(allowed)
        self.calls: list[str] = []

    async def authorize(self, request: Any, requirement: Any) -> Decision:
        self.calls.append(requirement.action)
        return Decision(requirement.action in self.allowed)


def routes(router: Any) -> dict[tuple[str, str], Any]:
    return {(r.methods[0], r.path): r.endpoint for r in router.routes}
