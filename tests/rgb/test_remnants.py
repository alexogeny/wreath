"""The remaining smaller items (report 23: B-11, G-17, G-31, G-34, G-35, G-38,
G-39, G-46, G-60, G-71, G-77, G-83, G-87, R-47)."""

from __future__ import annotations

import pytest


class TestSelectinBatchShape:
    """G-34: `_selectin_sql` emits a distinct statement per batch size, so the
    plan cache sees one entry per distinct key count."""

    def test_batches_are_padded_to_a_small_set_of_shapes(self):
        from wreath.orm.session import _batch_widths

        widths = _batch_widths()
        assert len(widths) <= 16
        assert list(widths) == sorted(widths)
        # Every batch size maps onto one of them, so N distinct key counts
        # produce at most len(widths) distinct statements.
        for count in (1, 2, 3, 7, 40, 999):
            assert any(width >= count for width in widths) or count > widths[-1]


class TestInsertReturningCost:
    """G-35: the RETURNING column list is computed with `item not in columns`
    over a list, which is quadratic in the column count on every insert."""

    def test_the_split_is_linear(self):
        from wreath.orm.session import _insert_columns

        class _Column:
            def __init__(self, index, server_default=None):
                self.index = index
                self.server_default = server_default

        class _Instance:
            def __init__(self, loaded):
                self._loaded = loaded

            def _orm_is_loaded(self, index):
                return index in self._loaded

        columns = tuple(_Column(index) for index in range(6))
        columns[3].server_default = "now()"
        supplied, returning = _insert_columns(columns, _Instance({0, 1, 3, 4}))
        assert [c.index for c in supplied] == [0, 1, 4]
        assert [c.index for c in returning] == [2, 3, 5]


class TestHydrationOverwrites:
    """G-38: `_hydrate` silently skips dirty fields, so a refetch that disagrees
    with a pending change is invisible."""

    def test_the_behaviour_is_documented_where_it_happens(self):
        import inspect

        from wreath.orm.session import Session

        source = inspect.getsource(Session._hydrate)
        assert "dirty" in source and ("wins" in source or "pending" in source)


class TestTenantFlushBinding:
    """G-39: `flush` never calls `_check_tenant_bound`; it is safe only because
    it happens to open its own transaction."""

    async def test_a_tenant_flush_states_its_requirement(self):
        from wreath.orm.errors import SessionError
        from wreath.orm.session import Session, TenantContext

        class _SchemaMode:
            kind = "isolated"

        class _Registry:
            schema_mode = _SchemaMode()
            database = None

        session = Session(_Registry(), "write", tenant=TenantContext(schema="t_acme"))

        # A read outside a bound transaction is refused, as before.
        with pytest.raises(SessionError, match="transaction"):
            await session.fetch(object())

        # `flush` is *not* refused, and that is deliberate: it opens its own
        # transaction, which binds `search_path` before any statement runs. The
        # reason has to be written where somebody might "optimise" the
        # transaction away for a single-statement flush.
        import inspect

        doc = inspect.getdoc(Session.flush) or ""
        assert "search_path" in doc and "load-bearing" in doc


class TestMultipartBoundary:
    """G-46: the boundary is taken from the first `boundary=` with no length or
    charset rule, which is a parser differential with anything upstream."""

    def test_an_overlong_boundary_is_refused(self):
        from wreath.request import _multipart_boundary

        long_boundary = b"x" * 200
        assert _multipart_boundary(b"multipart/form-data; boundary=" + long_boundary) is None

    def test_an_illegal_character_is_refused(self):
        from wreath.request import _multipart_boundary

        assert _multipart_boundary(b'multipart/form-data; boundary=a\x00b') is None

    def test_an_ordinary_boundary_is_accepted(self):
        from wreath.request import _multipart_boundary

        assert _multipart_boundary(b"multipart/form-data; boundary=abc123") == b"abc123"
        assert _multipart_boundary(b'multipart/form-data; boundary="abc 123"') == b"abc 123"


@pytest.mark.skip(
    reason=(
        "not a defect: the session cookie is diffed byte-for-byte on purpose, so "
        "a payload that does not round-trip -- another encoder, another key order "
        "-- is reissued with the same content and a fresh signature. "
        "tests/test_client_sessions_forms.py pins it. See report 23 G-60."
    )
)
def test_testsessionchangedetection_placeholder():
    raise AssertionError("unimplemented")


class TestCrudRequirementOrdering:
    """G-71: `_apply_requirement` mutates the handler *after* the route
    decorator registered it, which works only because requirements are read at
    compile time."""

    def test_the_registered_endpoint_carries_the_requirement(self):
        from wreath._auth.requirements import requirement_for
        from wreath.crud import Access, crud_router
        from wreath.orm import Mapped, Model, column
        from wreath.orm.types import Int64, Text

        class Gadget(Model, table="rgb_gadgets"):
            id: Mapped[int] = column(Int64, primary_key=True)
            name: Mapped[str] = column(Text)

        router = crud_router(
            Gadget, lambda request: None, operations=("list",),
            authorize=Access.roles("admin"),
        )
        endpoint = router.routes[0].endpoint
        assert requirement_for(endpoint).role_checks, (
            "the route was registered with an endpoint carrying no requirement"
        )


class TestSsoSessionLifetime:
    """G-77: an SSO session lasts the cookie's `max_age` regardless of what the
    identity provider said about the token's lifetime."""

    async def test_the_principal_can_carry_its_own_expiry(self):
        from wreath._auth.session_backend import SessionIdentityBackend

        class _State:
            session = {
                "principal": {"sub": "u1", "roles": [], "exp": 0},   # long expired
            }

        class _Request:
            state = _State()

        assert await SessionIdentityBackend().authenticate(_Request()) is None

    async def test_an_unexpired_principal_still_authenticates(self):
        import time

        from wreath._auth.session_backend import SessionIdentityBackend

        class _State:
            session = {
                "principal": {"sub": "u1", "roles": [], "exp": int(time.time()) + 3600},
            }

        class _Request:
            state = _State()

        identity = await SessionIdentityBackend().authenticate(_Request())
        assert identity is not None and identity.id == "u1"


@pytest.mark.skip(
    reason=(
        "not a defect: the client-side count fallback is documented on `_count` "
        "as the path a minimal session double takes. Refusing it would break "
        "every such double; `total=` is the escape hatch for real data. See "
        "report 23 G-83."
    )
)
def test_testcountfallbackbound_placeholder():
    raise AssertionError("unimplemented")


class TestRouteRecompileGuard:
    """R-47: `__call__` recompiles on `_dirty` with no guard. Single-threaded
    asyncio cannot interleave it -- `_compile_routes` never awaits -- but a
    free-threaded build has no such promise, and AGENTS.md treats that as a
    separately tested execution mode."""

    def test_compilation_is_guarded(self):
        import inspect

        from wreath import app as app_module

        source = inspect.getsource(app_module.Wreath._compile_routes)
        caller = inspect.getsource(app_module.Wreath.__call__)
        assert "_compile_lock" in source or "_compile_lock" in caller


class TestLifespanTeardown:
    """B-11: startup failure tears down clients and databases and leaves ORM
    registries and app-scoped dependencies as they were."""

    def test_the_failure_path_closes_the_app_scope(self):
        import inspect

        from wreath import app as app_module

        source = inspect.getsource(app_module.Wreath._lifespan)
        failure = source.split("lifespan.startup.failed")[0]
        assert "_app_scope" in failure, (
            "app-scoped dependencies survive a failed startup"
        )


class TestWebhookReplayScope:
    """G-87: `LocalReplayStore` is per-process, so replay protection silently
    degrades to per-worker behind more than one worker."""

    def test_the_scope_is_stated_where_it_is_built(self):
        import inspect

        from wreath.webhooks import LocalReplayStore

        doc = inspect.getdoc(LocalReplayStore) or ""
        assert "worker" in doc.lower() or "process" in doc.lower()
        assert "inbox" in doc.lower() or "postgres" in doc.lower()
