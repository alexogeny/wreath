"""Seams between subsystems that were built without sight of each other.

Four features landed in one day -- an MCP server, second factors, Postgres-native
retrieval, and a generated capability map -- each written by someone who could
not see the others. Every test here joins two of them, and each one exists
because the composition was *implied* by the code and asserted by nobody, which
is the state in which a composition quietly stops working.

Two claims are proved here:

* **Step-up reaches an MCP tool.** `Tool.requirement` is a whole
  `AuthRequirement`, and `AuthRequirement.second_factor` is what
  `wreath.auth.second_factor` writes onto a route. A route behind step-up that
  is exposed with `expose_routes` is therefore behind step-up as a tool -- and
  the *claim key* `wreath.users` stamps on the session is the one
  `wreath._mcp.server` reads, across three modules that never mention each other.
* **A retrieval tool is one tool.** A hybrid `Queries` fusion over pgvector and
  a `tsvector`, exposed as an MCP tool, gated on a Cedar action, bounded by a
  per-caller rate limit, and recorded on the Flight Recorder -- all of which are
  properties the tool inherits rather than properties its handler implements.

`docs/plans/cross-feature-integration.md` records the seams that do *not* hold
up, and why, so this file is only the half that does.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from wreath import Wreath
from wreath import logging as log
from wreath._auth.requirements import requirement_for
from wreath._mcp.registry import build_tool
from wreath.auth import Identity, SessionIdentityBackend, second_factor
from wreath.authorization import CedarAuthorizer
from wreath.mcp import MCP, PROTOCOL_VERSION, ToolRateLimit, expose_routes
from wreath.middleware.sessions import SessionMiddleware
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text, TsVector, Vector
from wreath.queries import Param, Queries, fuse, query
from wreath.testing import TestClient

NOW = 1_700_000_000.0


# -- the MCP harness, shared -------------------------------------------------


def _session_id(response: Any) -> str:
    return dict(response.headers)[b"mcp-session-id"].decode()


async def initialize(client: TestClient, **headers: str) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
        headers=headers,
    )
    return _session_id(response)


async def call_tool(
    client: TestClient, session: str, name: str, arguments: dict, **headers: str
) -> dict:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"mcp-session-id": session, **headers},
    )
    return response.json()


def refusal(answer: dict) -> str:
    """The text of a refused `tools/call`, whichever shape it came back in."""
    if "error" in answer:
        return str(answer["error"].get("message", "")) + str(answer["error"].get("data", ""))
    content = answer["result"].get("content", ())
    return " ".join(str(item.get("text", "")) for item in content)


# ===========================================================================
# Step-up x MCP
# ===========================================================================


class _Backend:
    """A bearer backend that hands back one prepared identity, or nobody."""

    def __init__(self, identity: Identity | None) -> None:
        self.identity = identity

    async def authenticate(self, request: Any) -> Identity | None:
        return self.identity

    def challenge(self, request: Any) -> str | None:
        return None


def stepup_app(identity: Identity | None) -> tuple[Wreath, MCP, list[str]]:
    """One route behind `@second_factor`, exposed as a tool."""
    ran: list[str] = []
    app = Wreath()
    app.configure_auth(_Backend(identity))

    @app.delete("/sightings", tags=("sightings",))
    @second_factor(max_age=300)
    async def purge_sightings(request) -> dict:
        """Delete every sighting."""
        ran.append("purge_sightings")
        return {"purged": True}

    mcp = MCP(app, name="camera-trap", version="1.0.0")
    expose_routes(mcp, app, tags=("sightings",))
    return app, mcp, ran


def test_a_step_up_route_carries_its_window_onto_the_tool() -> None:
    """The compiled requirement, before any request touches it.

    `expose_routes` merges the route's inherited requirement with the endpoint's
    own decorators, so the seam is visible statically: if `@second_factor` ever
    stopped producing an `AuthRequirement`, this fails without needing a caller.
    """
    _, mcp, _ = stepup_app(None)
    tool = next(entry for entry in mcp.tools if entry.name == "purge_sightings")
    assert tool.requirement.second_factor == 300.0
    assert tool.requirement.authenticated is True


async def test_a_caller_who_never_proved_a_factor_cannot_call_the_tool() -> None:
    """A bearer identity has no stamp, and an absent stamp is a refusal."""
    app, mcp, ran = stepup_app(Identity(id="ada", claims={"sub": "ada"}))
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert "second factor" in refusal(answer)
    assert ran == []
    # Counted as a refusal, never as a tool that failed: an operator reading
    # `tool_errors` must not see a policy decision in it.
    assert mcp.unauthorized_calls == 1
    assert mcp.tool_errors == 0


async def test_a_caller_who_proved_a_factor_recently_may_call_it() -> None:
    app, mcp, ran = stepup_app(
        Identity(id="ada", claims={"sub": "ada", "second_factor_at": int(time.time())})
    )
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert answer["result"]["structuredContent"] == {"purged": True}
    assert ran == ["purge_sightings"]
    assert mcp.unauthorized_calls == 0


async def test_a_factor_proved_too_long_ago_is_refused() -> None:
    """Recency is the whole point: holding a factor is not having proved one."""
    stale = int(time.time()) - 400
    app, mcp, ran = stepup_app(
        Identity(id="ada", claims={"sub": "ada", "second_factor_at": stale})
    )
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert "second factor" in refusal(answer)
    assert ran == []


async def test_the_stamp_wreath_users_writes_is_the_claim_the_tool_reads() -> None:
    """The whole path, across three modules that never mention each other.

    `wreath.users.second_factor_router` writes `second_factor_at` onto the
    session principal; `SessionIdentityBackend` copies the principal into
    `Identity.claims`; `wreath._mcp.server` reads it back through
    `second_factor_age`. Nothing in the tree asserts that those three agree on
    the key, and a rename in any one of them would be silent everywhere else.
    """
    ran: list[str] = []
    app = Wreath()
    app.add_global_middleware(SessionMiddleware(secret="s" * 32, secure=False))
    app.configure_auth(SessionIdentityBackend())

    @app.post("/sessions", tags=("public",))
    async def sign_in(request) -> dict:
        """Sign in, with no second factor proved yet."""
        request.state.session["principal"] = {"sub": "ada"}
        return {"ok": True}

    @app.post("/sessions/factor", tags=("public",))
    async def prove_factor(request) -> dict:
        """Record a freshly proved second factor on the session principal."""
        principal = dict(request.state.session["principal"])
        # Exactly the shape `wreath.users._stamp` writes after `POST
        # /auth/2fa/verify`: the key, and Unix seconds.
        principal["second_factor_at"] = int(time.time())
        request.state.session["principal"] = principal
        return {"ok": True}

    @app.delete("/sightings", tags=("sightings",))
    @second_factor(max_age=300)
    async def purge_sightings(request) -> dict:
        """Delete every sighting."""
        ran.append("purge_sightings")
        return {"purged": True}

    mcp = MCP(app, name="camera-trap", version="1.0.0")
    expose_routes(mcp, app, tags=("sightings",))

    async with TestClient(app) as client:
        response = await client.post("/sessions", json={})
        issued = response.header("set-cookie")
        assert issued is not None
        cookie = issued.split(";", 1)[0]

        # Signed in, and refused: a session that never proved a factor is a
        # perfectly good identity that has not stepped up.
        session = await initialize(client, cookie=cookie)
        assert "second factor" in refusal(
            await call_tool(client, session, "purge_sightings", {}, cookie=cookie)
        )
        assert ran == []

        response = await client.post("/sessions/factor", json={}, headers={"cookie": cookie})
        cookie = (response.header("set-cookie") or cookie).split(";", 1)[0]
        session = await initialize(client, cookie=cookie)
        answer = await call_tool(client, session, "purge_sightings", {}, cookie=cookie)

    assert answer["result"]["structuredContent"] == {"purged": True}
    assert ran == ["purge_sightings"]


class _AgeEngine:
    """Permits the action only when the context says a factor is fresh."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.seen: list[dict] = []

    def is_authorized(self, **request: Any) -> bool:
        context = dict(request["context"])
        self.seen.append(context)
        age = context.get("second_factor_age")
        return age is not None and age <= self.window


async def test_a_cedar_gated_tool_sees_the_second_factor_age_in_its_context() -> None:
    """The other half of the seam, and the more expressive one.

    `CedarAuthorizer`'s default context mapper publishes `second_factor_age`,
    and the MCP server runs policies through the *same* authorizer as a route --
    so a policy of the form `when { context.second_factor_age <= 300 }` gates a
    tool with no MCP-specific code anywhere. The key is absent rather than a
    sentinel when no factor was proved, which is what makes it fail closed.
    """
    engine = _AgeEngine(window=300)

    def build(identity: Identity) -> tuple[Wreath, MCP, list[str]]:
        ran: list[str] = []
        app = Wreath()
        app.configure_auth(
            _Backend(identity),
            CedarAuthorizer(
                engine=engine,
                principal=lambda identity: f"User::{identity.id}",
                action=lambda action, request: action,
                resource=lambda resource, request: resource,
                entities=lambda request: (),
            ),
        )
        mcp = MCP(app, name="camera-trap", version="1.0.0")

        @mcp.tool(action="Sighting::purge", resource="all")
        async def purge_sightings(request) -> dict:
            """Delete every sighting."""
            ran.append("purge_sightings")
            return {"purged": True}

        return app, mcp, ran

    fresh = Identity(id="ada", claims={"second_factor_at": int(time.time())})
    app, _, ran = build(fresh)
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert answer["result"]["structuredContent"] == {"purged": True}
    assert engine.seen[-1]["second_factor_age"] <= 1

    app, _, ran = build(Identity(id="ada", claims={}))
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert "may not" in refusal(answer)
    assert ran == []
    # Absent, not zero and not a sentinel: `context has second_factor_age` is
    # false, so both a `when` and an `unless` policy fail closed.
    assert "second_factor_age" not in engine.seen[-1]


def declared_stepup_app(
    identity: Identity | None, *, window: float = 300.0
) -> tuple[Wreath, MCP, list[str]]:
    """A tool that was never a route, asking for step-up in its own right."""
    ran: list[str] = []
    app = Wreath()
    app.configure_auth(_Backend(identity))
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(second_factor=window)
    async def purge_sightings(request) -> dict:
        """Delete every sighting."""
        ran.append("purge_sightings")
        return {"purged": True}

    return app, mcp, ran


def test_a_declared_tool_can_ask_for_step_up_of_its_own() -> None:
    """`second_factor=` compiles to the requirement `@second_factor` produces.

    Before this keyword existed, step-up reached a tool only by exposing a route
    that already carried it, so "this tool deletes things, ask for the code
    again" required inventing a route to hang it on. The compiled requirement is
    checked statically here, and enforced in the three tests below.
    """
    _, mcp, _ = declared_stepup_app(None)
    tool = next(entry for entry in mcp.tools if entry.name == "purge_sightings")
    assert tool.requirement.second_factor == 300.0
    # Implied, exactly as `add_second_factor` implies it on a route: a window is
    # measured against an identity, so there has to be one.
    assert tool.requirement.authenticated is True


async def test_a_declared_step_up_tool_refuses_a_caller_with_no_factor() -> None:
    app, mcp, ran = declared_stepup_app(Identity(id="ada", claims={"sub": "ada"}))
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert "second factor" in refusal(answer)
    assert ran == []
    assert mcp.unauthorized_calls == 1
    assert mcp.tool_errors == 0


async def test_a_declared_step_up_tool_admits_a_caller_who_proved_one() -> None:
    app, _, ran = declared_stepup_app(
        Identity(id="ada", claims={"sub": "ada", "second_factor_at": int(time.time())})
    )
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert answer["result"]["structuredContent"] == {"purged": True}
    assert ran == ["purge_sightings"]


async def test_a_declared_step_up_tool_refuses_a_stale_factor() -> None:
    """Recency, on the declared path as much as on the exposed one."""
    app, _, ran = declared_stepup_app(
        Identity(id="ada", claims={"sub": "ada", "second_factor_at": int(time.time()) - 400})
    )
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(client, session, "purge_sightings", {})
    assert "second factor" in refusal(answer)
    assert ran == []


def test_an_exposed_route_keeps_the_shorter_window_the_tool_declares() -> None:
    """Merging adds, in both directions.

    `expose_routes` hands the route's requirement to `build_tool`, and a
    `second_factor=` on the declaration must not be able to *relax* what the
    route already asked for -- nor the other way round. Both orders end at the
    minimum, because that is what `merge_requirements` promises and this is the
    one call site that could have applied the keyword after the merge.
    """
    app = Wreath()

    @app.delete("/sightings", tags=("sightings",))
    @second_factor(max_age=60)
    async def purge_sightings(request) -> dict:
        """Delete every sighting."""
        return {"purged": True}

    inherited = requirement_for(purge_sightings)
    relaxed = build_tool(
        purge_sightings, requirement=inherited, second_factor=3600, route="/sightings"
    )
    assert relaxed.requirement.second_factor == 60.0

    tightened = build_tool(
        purge_sightings, requirement=inherited, second_factor=10, route="/sightings"
    )
    assert tightened.requirement.second_factor == 10.0


def test_a_window_that_can_never_be_satisfied_is_refused() -> None:
    """Zero is a typo, not a policy of never admitting anyone."""
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    with pytest.raises(ValueError, match="must be positive"):

        @mcp.tool(second_factor=0)
        async def purge(request) -> dict:
            """Delete every sighting."""
            return {}


# ===========================================================================
# The caller, resolved before the controls that name them
# ===========================================================================
#
# An endpoint with no `MCPAuth` and an `app.configure_auth(...)` backend is the
# configuration `expose_routes` exists for, and on it the identity is resolved
# lazily -- inside `_authorize`, which runs *after* the rate limit is charged
# and after the audit marker names the caller. Both tests below failed before
# that resolution moved ahead of them.


def _allow_everything() -> CedarAuthorizer:
    class _Yes:
        def is_authorized(self, **request: Any) -> bool:
            return True

    return CedarAuthorizer(
        engine=_Yes(),
        principal=lambda identity: f"User::{identity.id}",
        action=lambda action, request: action,
        resource=lambda resource, request: resource,
        entities=lambda request: (),
    )


async def test_a_fresh_session_does_not_reset_a_tools_ceiling() -> None:
    """A per-tool ceiling is per *caller*, and `initialize` is free.

    `_throttle` keys the bucket on the verified subject when there is one and on
    the session otherwise. A model opens a session per turn, so a bucket keyed on
    the session is no bucket at all: the ceiling of one call an hour admitted six
    before the caller was resolved ahead of the charge.
    """
    ran: list[str] = []
    app = Wreath()
    app.configure_auth(_Backend(Identity(id="ada", claims={"sub": "ada"})))
    mcp = MCP(app, name="notebook", version="1.0.0")

    @mcp.tool(rate_limit=ToolRateLimit(limit=1, window=3600.0))
    async def scan(request) -> dict:
        """Walk the whole corpus."""
        ran.append("scan")
        return {"scanned": True}

    async with TestClient(app) as client:
        first = await call_tool(client, await initialize(client), "scan", {})
        refusals = [
            refusal(await call_tool(client, await initialize(client), "scan", {}))
            for _ in range(5)
        ]

    assert first["result"]["structuredContent"] == {"scanned": True}
    assert all("retry in" in text for text in refusals), refusals
    assert ran == ["scan"]
    assert mcp.tool_calls == 1
    assert mcp.throttled == 5


class _HeaderBackend:
    """Authenticates the caller only when they actually present the header."""

    async def authenticate(self, request: Any) -> Identity | None:
        if request.header("x-token") == "ada":
            return Identity(id="ada", claims={"sub": "ada"})
        return None

    def challenge(self, request: Any) -> str | None:
        return None


async def test_withholding_credentials_from_initialize_does_not_reset_the_ceiling() -> None:
    """The case the session-ownership check cannot cover, and the only test that does.

    `_owns` re-resolves the caller on every message -- but only when the session
    has a principal to compare against. A session opened *without* credentials
    binds nobody, so `_owns` short-circuits to `True` and resolves no identity at
    all. A caller who holds a token and simply does not send it on `initialize`
    therefore reaches `tools/call` unidentified, and the bucket keys on a session
    id that `initialize` mints for free.

    The eager `await self._identify(request)` at the top of `_tools_call` is what
    closes that, and this is the shape that proves it: the test above authenticates
    every request, including `initialize`, so `_owns` has already done the work
    there and the eager resolution is redundant on that path. Delete the eager
    call with only that test watching and six calls get through a ceiling of one.
    """
    ran: list[str] = []
    app = Wreath()
    app.configure_auth(_HeaderBackend())
    mcp = MCP(app, name="notebook", version="1.0.0")

    @mcp.tool(rate_limit=ToolRateLimit(limit=1, window=3600.0))
    async def scan(request) -> dict:
        """Walk the whole corpus."""
        ran.append("scan")
        return {"scanned": True}

    async with TestClient(app) as client:
        # No credentials on `initialize`; the token arrives only with the call.
        first = await call_tool(client, await initialize(client), "scan", {}, **{"x-token": "ada"})
        refusals = [
            refusal(
                await call_tool(
                    client, await initialize(client), "scan", {}, **{"x-token": "ada"}
                )
            )
            for _ in range(5)
        ]

    assert first["result"]["structuredContent"] == {"scanned": True}
    assert all("retry in" in text for text in refusals), refusals
    assert ran == ["scan"]
    assert mcp.tool_calls == 1
    assert mcp.throttled == 5


async def test_the_marker_names_the_caller_the_request_authenticated() -> None:
    """`principal` is "the subject the caller's own token asserts", or it is nothing.

    The marker exists so an operator can answer "on whose behalf" six months
    later. On an endpoint with no `MCPAuth` it used to answer "anonymous" for a
    caller the very same request authenticated and then authorized as `ada`.
    """
    app = Wreath()
    app.configure_auth(_Backend(Identity(id="ada", claims={"sub": "ada"})), _allow_everything())
    mcp = MCP(app, name="notebook", version="1.0.0")

    @mcp.tool(action="Note::search", resource="notes")
    async def search_notes(request, terms: str) -> dict:
        """Find notes matching `terms`."""
        # The context the handler reads names the caller too, on this path.
        assert request.state.mcp.identity is not None
        return {"caller": request.state.mcp.identity.id}

    from wreath import _flight_schema as fs

    with log.testing_runtime() as records, log.request_scope(request_id=12) as scope:
        async with TestClient(app) as client:
            session = await initialize(client)
            answer = await call_tool(client, session, "search_notes", {"terms": "llamas"})
        scope.finish(promoted=True)
        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]

    assert answer["result"]["structuredContent"] == {"caller": "ada"}
    marker = next(entry for entry in markers if entry.get("tool") == "search_notes")
    assert marker["outcome"] == "ok"
    assert marker["principal"] == "ada"


# ===========================================================================
# Retrieval x MCP
# ===========================================================================

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_crossfeature_{_WORKER}"

_live = pytest.mark.skipif(
    _DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for the retrieval-tool tests"
)


class Note(Model, table="notes", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(Vector(3))
    search: Mapped[bytes] = column(
        TsVector("english", sources=("title", "body")), index="gin"
    )


class Notes(Queries[Note]):
    """The shape of an application's retrieval layer: two searches and a fusion."""

    nearest = query().order_by(Note.embedding.cosine_distance(Param("q"))).limit(3)
    matching = (
        query(Note.search.matches(Param("terms")))
        .order_by(Note.search.rank(Param("terms")).desc())
        .limit(3)
    )
    hybrid = fuse(nearest, matching).limit(4)


#: The same corpus and the same hand-computed answer as `tests/orm/
#: test_hybrid_search.py`, so a failure here is about the MCP half.
_ROWS = [
    (1, "Alpaca grooming", "brushing an alpaca", "[1,0,0]"),
    (2, "Llama husbandry", "keeping llamas well", "[0.8,0.6,0]"),
    (3, "Tractor upkeep", "diesel and grease", "[0.6,0.8,0]"),
    (4, "Trailer fittings", "a trailer for llamas", "[0,0,1]"),
]


@pytest.fixture
async def notes() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for the retrieval-tool tests")
    from wreath.orm.introspection import resolve_extension_types
    from wreath.orm.types import _unbind_extension_oids
    from wreath.postgres import Database as PgDatabase
    from wreath.postgres import PoolConfig

    database = PgDatabase(
        "cross-feature", _DSN, pools={"write": PoolConfig(min_size=1, max_size=3)}
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        try:
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 - reported as a skip on the next line
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."notes" ('
            " id bigint primary key,"
            " title text not null,"
            " body text not null,"
            " embedding vector(3) not null,"
            " search tsvector generated always as ("
            "   to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))"
            " ) stored not null)"
        )
        await connection.execute(
            f'CREATE INDEX ON "{_SCHEMA}"."notes" USING gin (search)'
        )
        for identifier, title, body, embedding in _ROWS:
            await connection.execute(
                f'INSERT INTO "{_SCHEMA}"."notes" (id, title, body, embedding) '
                f"VALUES ($1, $2, $3, '{embedding}'::vector)",
                identifier,
                title,
                body,
            )
    finally:
        await database.release("write", connection)
    built = Registry(database, [Note], validate_schema="off")
    _unbind_extension_oids()
    await resolve_extension_types(built)
    try:
        yield built
    finally:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        finally:
            await database.release("write", connection)
        await database.stop()


class _SearchEngine:
    """Permits `Note::search` for anyone holding the `reader` role."""

    def __init__(self) -> None:
        self.asked: list[Any] = []

    def is_authorized(self, **request: Any) -> bool:
        self.asked.append(request["action"])
        return request["action"] == "Note::search"


def retrieval_app(
    registry: Any,
    identity: Identity | None,
    *,
    rate_limit: ToolRateLimit | None = None,
) -> tuple[Wreath, MCP, _SearchEngine]:
    """The application shape this whole item is about: one retrieval tool."""
    from wreath.orm.session import Session

    engine = _SearchEngine()
    app = Wreath()
    app.configure_auth(
        _Backend(identity),
        CedarAuthorizer(
            engine=engine,
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
        ),
    )
    mcp = MCP(app, name="notebook", version="1.0.0")

    @mcp.tool(action="Note::search", resource="notes", rate_limit=rate_limit)
    async def search_notes(request, terms: str, q: list[float]) -> dict:
        """Find notes matching `terms`, ranked with their similarity to `q`."""
        session = Session(registry, "write")
        try:
            found = await Notes(session).hybrid(q=q, terms=terms)
            return {"notes": [{"id": item.id, "title": item.title} for item in found]}
        finally:
            await session.close()

    @mcp.tool(action="Note::purge", resource="notes")
    async def purge_notes(request) -> dict:
        """Delete every note."""
        return {"purged": True}

    return app, mcp, engine


@_live
@pytest.mark.database
async def test_a_hybrid_search_is_reachable_as_one_mcp_tool(notes: Any) -> None:
    """The composition, end to end, against a real PostgreSQL.

    `[2, 1, 4, 3]` is the fused order neither half produces alone -- pinned in
    `tests/orm/test_hybrid_search.py` against hand-computed reciprocal-rank
    scores -- so this asserts the ranking survived the whole MCP round trip and
    not merely that some rows came back.
    """
    app, mcp, _ = retrieval_app(notes, Identity(id="ada"))
    async with TestClient(app) as client:
        session = await initialize(client)
        answer = await call_tool(
            client,
            session,
            "search_notes",
            {"terms": "llamas", "q": [1.0, 0.0, 0.0]},
        )
    ranked = [note["id"] for note in answer["result"]["structuredContent"]["notes"]]
    assert ranked == [2, 1, 4, 3]
    assert mcp.tool_calls == 1
    assert mcp.tool_errors == 0


@_live
@pytest.mark.database
async def test_the_retrieval_tool_is_behind_the_same_cedar_decision_as_a_route(
    notes: Any,
) -> None:
    """The gate is a declaration on the tool, not a check in the handler.

    The same server exposes a second tool the policy refuses, so this is about
    the decision and not about the authorizer being wired at all.
    """
    app, mcp, engine = retrieval_app(notes, Identity(id="ada"))
    async with TestClient(app) as client:
        session = await initialize(client)
        refused = await call_tool(client, session, "purge_notes", {})
        allowed = await call_tool(
            client, session, "search_notes", {"terms": "llamas", "q": [1.0, 0.0, 0.0]}
        )
    assert "may not" in refusal(refused)
    assert allowed["result"]["structuredContent"]["notes"]
    assert engine.asked == ["Note::purge", "Note::search"]
    assert mcp.unauthorized_calls == 1


@_live
@pytest.mark.database
async def test_the_retrieval_tool_can_be_bounded_per_caller(notes: Any) -> None:
    """A search is the expensive tool on the server, and the bound is a keyword.

    A model does not click. One `rate_limit=` on the declaration is the whole
    difference between a retrieval tool and a way to make an application run
    unbounded vector scans.
    """
    app, mcp, _ = retrieval_app(
        notes, Identity(id="ada"), rate_limit=ToolRateLimit(limit=1, window=3600.0)
    )
    async with TestClient(app) as client:
        session = await initialize(client)
        arguments = {"terms": "llamas", "q": [1.0, 0.0, 0.0]}
        first = await call_tool(client, session, "search_notes", arguments)
        second = await call_tool(client, session, "search_notes", arguments)
    assert first["result"]["structuredContent"]["notes"]
    assert "too often" in refusal(second).lower() or "retry" in refusal(second).lower()
    assert mcp.throttled == 1
    assert mcp.tool_calls == 1


@_live
@pytest.mark.database
async def test_the_search_the_model_asked_for_is_on_the_audit_trail(notes: Any) -> None:
    """What a retrieval tool is asked for is the fact an audit needs six months on.

    The terms ride the canonical log line under `mcp.arg.`, fingerprinted rather
    than raw by `wreath.logging`'s deny-by-default rule -- which still answers
    "was this the same query" without publishing what somebody searched for.
    """
    app, _, _ = retrieval_app(notes, Identity(id="ada"))
    with log.testing_runtime() as records, log.request_scope(request_id=11) as scope:
        async with TestClient(app) as client:
            session = await initialize(client)
            await call_tool(
                client,
                session,
                "search_notes",
                {"terms": "llamas", "q": [1.0, 0.0, 0.0]},
            )
        scope.finish(promoted=True)
        from wreath import _flight_schema as fs

        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]
        fields: dict[str, Any] = {}
        for cell in records:
            if cell.flags & fs.LOG_FLAG_EVENT_FIELDS:
                fields.update(log.attributes(cell))

    marker = next(entry for entry in markers if entry.get("tool") == "search_notes")
    assert marker["outcome"] == "ok"
    assert fields["mcp.arg.terms"].startswith("#")
    assert "llamas" not in repr(fields)
    # On whose behalf, which is the other half of what an audit needs. This
    # endpoint carries no `MCPAuth`, so the identity comes from the application's
    # own backend -- resolved before the marker rather than after it.
    assert marker["principal"] == "ada"
