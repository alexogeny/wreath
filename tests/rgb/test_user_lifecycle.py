from __future__ import annotations

from wreath._userkit import (
    CapturingEmailSender,
    InMemoryUserStore,
    hash_password,
)


class _RecordingSessionStore:
    """A session store that can also say which sessions belong to a user."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.deleted_for: list[str] = []

    async def load(self, sid):
        return self.rows.get(sid)

    async def save(self, sid, data, max_age):
        self.rows[sid] = data

    async def delete(self, sid):
        self.rows.pop(sid, None)

    async def delete_for(self, subject: str) -> int:
        self.deleted_for.append(subject)
        gone = [
            sid
            for sid, data in self.rows.items()
            if (data.get("principal") or {}).get("sub") == subject
        ]
        for sid in gone:
            del self.rows[sid]
        return len(gone)


class TestResetEndsSessions:
    """G-75: a password reset leaves every existing session working, so the
    reset a user performs *because* somebody else is in their account does not
    remove them."""

    async def _reset(self, store, sessions, **kwargs):
        from wreath.users import reset_password_endpoint

        return await reset_password_endpoint(store, sessions=sessions, **kwargs)

    async def test_a_successful_reset_drops_that_users_sessions(self):
        users = InMemoryUserStore()
        user = await users.create("ann@example.com", hash_password("hunter2hunter2"))
        sessions = _RecordingSessionStore()
        sessions.rows["sid-ann"] = {"principal": {"sub": user.id}}
        sessions.rows["sid-other"] = {"principal": {"sub": "someone-else"}}

        from wreath._userkit import fingerprint, sign_token

        token = sign_token(
            "s" * 32,
            "reset",
            user.id,
            ttl=3600,
            bound=fingerprint(user.hashed_password),
        )
        done = await self._reset(
            users,
            sessions,
            secret="s" * 32,
            token=token,
            new_password="a-much-better-one",
        )
        assert done is True
        assert sessions.deleted_for == [user.id]
        assert "sid-ann" not in sessions.rows
        assert "sid-other" in sessions.rows, "another user's session was dropped"

    async def test_a_failed_reset_drops_nothing(self):
        users = InMemoryUserStore()
        await users.create("ann@example.com", hash_password("hunter2hunter2"))
        sessions = _RecordingSessionStore()
        sessions.rows["sid-ann"] = {"principal": {"sub": "1"}}

        done = await self._reset(
            users,
            sessions,
            secret="s" * 32,
            token="not-a-token",
            new_password="a-much-better-one",
        )
        assert done is False
        assert sessions.deleted_for == []
        assert "sid-ann" in sessions.rows

    async def test_a_store_that_cannot_enumerate_is_tolerated(self):
        users = InMemoryUserStore()
        user = await users.create("ann@example.com", hash_password("hunter2hunter2"))

        class _Minimal:
            async def load(self, sid):  # pragma: no cover
                return None

            async def save(self, sid, data, max_age):  # pragma: no cover
                pass

            async def delete(self, sid):  # pragma: no cover
                pass

        from wreath._userkit import fingerprint, sign_token

        token = sign_token(
            "s" * 32,
            "reset",
            user.id,
            ttl=3600,
            bound=fingerprint(user.hashed_password),
        )
        assert (
            await self._reset(
                users,
                _Minimal(),
                secret="s" * 32,
                token=token,
                new_password="a-much-better-one",
            )
            is True
        )

    async def test_no_session_store_still_resets(self):
        users = InMemoryUserStore()
        user = await users.create("ann@example.com", hash_password("hunter2hunter2"))
        from wreath._userkit import fingerprint, sign_token

        token = sign_token(
            "s" * 32,
            "reset",
            user.id,
            ttl=3600,
            bound=fingerprint(user.hashed_password),
        )
        assert (
            await self._reset(
                users,
                None,
                secret="s" * 32,
                token=token,
                new_password="a-much-better-one",
            )
            is True
        )


class TestPostgresSessionStoreEnumeration:
    """The shipped store needs the capability the router asks for."""

    async def test_delete_for_issues_one_statement(self):
        from wreath.session_store import PostgresSessionStore

        executed: list[tuple[str, tuple]] = []

        class _Statement:
            async def execute(self, *args):
                executed.append(("execute", args))
                return "DELETE 2"

        class _Database:
            def statement(self, name, sql, workload="write"):
                executed.append((sql, ()))
                return _Statement()

        store = PostgresSessionStore(_Database())
        removed = await store.delete_for("u1")
        assert removed == 2
        assert any("DELETE" in entry[0] for entry in executed)


class TestLoginThrottling:
    """B-14: nothing counts failed logins, so password guessing is limited only
    by how fast the attacker can send requests."""

    def _router(self, **kwargs):
        from wreath.users import user_router

        store = InMemoryUserStore()
        return store, user_router(
            store, secret="s" * 32, email_sender=CapturingEmailSender(), **kwargs
        )

    async def test_the_router_accepts_the_throttle_settings(self):
        store, router = self._router(max_login_attempts=3, login_window=60.0)
        assert router is not None
        await store.create("ann@example.com", hash_password("hunter2hunter2"))

    async def test_the_limiter_refuses_after_the_budget(self):
        from wreath.users import LoginLimiter

        limiter = LoginLimiter(max_attempts=3, window=60.0)
        for _ in range(3):
            assert limiter.allow("ann@example.com") is True
            limiter.record_failure("ann@example.com")
        assert limiter.allow("ann@example.com") is False

    async def test_a_success_clears_the_count(self):
        from wreath.users import LoginLimiter

        limiter = LoginLimiter(max_attempts=3, window=60.0)
        limiter.record_failure("ann@example.com")
        limiter.record_failure("ann@example.com")
        limiter.record_success("ann@example.com")
        for _ in range(3):
            assert limiter.allow("ann@example.com") is True
            limiter.record_failure("ann@example.com")
        assert limiter.allow("ann@example.com") is False

    async def test_one_account_cannot_lock_another_out(self):
        from wreath.users import LoginLimiter

        limiter = LoginLimiter(max_attempts=2, window=60.0)
        for _ in range(3):
            limiter.record_failure("ann@example.com")
        assert limiter.allow("ann@example.com") is False
        assert limiter.allow("bo@example.com") is True

    async def test_the_window_expires(self):
        from wreath.users import LoginLimiter

        clock = [0.0]
        limiter = LoginLimiter(max_attempts=1, window=10.0, clock=lambda: clock[0])
        limiter.record_failure("ann@example.com")
        assert limiter.allow("ann@example.com") is False
        clock[0] = 11.0
        assert limiter.allow("ann@example.com") is True

    def test_the_primitive_is_still_unguarded(self):
        import inspect

        from wreath import _userkit

        doc = inspect.getdoc(_userkit.authenticate) or ""
        assert "throttl" in doc.lower() or "rate" in doc.lower()
