"""Dependency binding, path placeholders, and optional identity.

Three gaps the canonical example hit while being built, each of which forced it
into a workaround it had to document as forced rather than chosen. Two are
members of the family the refuse-rather-than-half-wire rule in `AGENTS.md`
names -- a declaration accepted at registration that then fails per request as a
status blaming the caller -- and the third is a missing spelling rather than a
wrong one.

The over-refusal guards carry as much weight as the refusals here. A check that
rejects a correct declaration fails at import and takes the whole application
with it, which is worse than the runtime error it replaced.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest

import wreath
from wreath.auth import AuthenticationBackend, Identity, authenticated, identify
from wreath.binding import Depends, Query
from wreath.pagination import DEFAULT_SIZE, MAX_PAGE, MAX_SIZE, PageParams, page_params
from wreath.request import Request
from wreath.router import Router
from wreath.testing import TestClient

# --- path converters ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_path_converter_greedily_binds_trailing_segments() -> None:
    app = wreath.Wreath()

    @app.get("/media/{key:path}")
    async def handler(request: Request, key: str) -> dict:
        return {"key": key}

    async with TestClient(app) as client:
        response = await client.get("/media/images/llama.jpg")

    assert response.json() == {"key": "images/llama.jpg"}


@pytest.mark.asyncio
async def test_a_router_can_declare_a_trailing_path_converter() -> None:
    router = Router(prefix="/api")

    @router.get("/media/{key:path}")
    async def handler(request: Request, key: str) -> dict:
        return {"key": key}

    app = wreath.Wreath()
    app.include_router(router)
    async with TestClient(app) as client:
        response = await client.get("/api/media/a/b")

    assert response.json() == {"key": "a/b"}


def test_an_unknown_converter_is_refused_at_registration() -> None:
    app = wreath.Wreath()
    with pytest.raises(ValueError, match="unknown path converter"):

        @app.get("/media/{key:rest}")
        async def handler(request: Request) -> dict:  # pragma: no cover
            return {}


def test_an_empty_converted_placeholder_is_refused() -> None:
    app = wreath.Wreath()
    with pytest.raises(ValueError, match="empty path placeholder"):

        @app.get("/media/{:path}")
        async def handler(request: Request) -> dict:  # pragma: no cover
            return {}


def test_a_path_converter_must_be_the_final_segment() -> None:
    app = wreath.Wreath()
    with pytest.raises(ValueError, match="final path segment"):

        @app.get("/media/{key:path}/metadata")
        async def handler(request: Request) -> dict:  # pragma: no cover
            return {}


@pytest.mark.parametrize(
    "path",
    [
        "/literal/{open",
        "/literal/close}",
        "/literal/{open:path",
        "/literal/close:path}",
    ],
)
def test_unpaired_braces_are_refused_consistently(path: str) -> None:
    app = wreath.Wreath()
    with pytest.raises(ValueError, match="entire segment"):

        @app.get(path)
        async def handler(request: Request) -> dict:  # pragma: no cover
            return {}


def test_an_empty_placeholder_is_refused() -> None:
    app = wreath.Wreath()
    with pytest.raises(ValueError, match="empty path placeholder"):

        @app.get("/a/{}/b")
        async def handler(request: Request) -> dict:  # pragma: no cover
            return {}


def test_an_ordinary_placeholder_still_registers() -> None:
    """Over-refusal guard: the check must not reject what already worked."""
    app = wreath.Wreath()

    @app.get("/llamas/{id}")
    async def handler(request: Request, id: int) -> dict:
        return {"id": id}

    app._compile_routes()


# --- Depends inside Annotated: silently rebound as a request body -------------


async def _one(request: Request) -> int:  # pragma: no cover
    """Module scope on purpose: handler annotations are strings under
    `from __future__ import annotations`, and `get_type_hints` cannot resolve a
    dependency defined inside the test function."""
    return 1


def test_depends_inside_annotated_is_refused() -> None:
    """The worse of the two spellings, because it blames the caller.

    `Depends` is read from the parameter default only, so inside `Annotated` it
    was invisible and the parameter fell through to body binding -- a GET
    answered `400 "invalid JSON body"`. A 500 says *we* broke; a 400 says *you*
    broke, and nothing in the response tells the caller which it was.
    """
    app = wreath.Wreath()

    @app.get("/a")
    async def handler(
        request: Request, value: Annotated[int, Depends(_one)]
    ) -> dict:  # pragma: no cover
        return {}

    with pytest.raises(TypeError) as caught:
        app._compile_routes()

    message = str(caught.value)
    assert "Depends() inside Annotated" in message
    assert "= Depends(...)" in message, "the refusal must show the default spelling"


def test_depends_as_a_default_still_works() -> None:
    """Over-refusal guard: `Depends` genuinely is written as a default."""

    async def dependency(request: Request) -> int:
        return 7

    app = wreath.Wreath()

    @app.get("/a")
    async def handler(request: Request, value: int = Depends(dependency)) -> dict:
        return {"value": value}

    app._compile_routes()


# --- a binding marker on a dependency's own parameter ------------------------


def test_a_marker_on_the_first_dependency_parameter_is_refused() -> None:
    """The request object arrives *as* that value, and the next compare is a 500.

    A dependency is called `fn(request, **nested_depends)` and nothing else, so
    a `Query()` on its first parameter does not bind -- it receives the request.
    """

    def dependency(page: Annotated[int, Query(minimum=1)] = 1) -> int:  # pragma: no cover
        return page

    app = wreath.Wreath()

    @app.get("/a")
    async def handler(request: Request, value: int = Depends(dependency)) -> dict:
        return {}  # pragma: no cover

    with pytest.raises(TypeError) as caught:
        app._compile_routes()

    assert "wreath fills with the request itself" in str(caught.value)


def test_a_marker_on_a_later_dependency_parameter_is_refused() -> None:
    """Quieter and worse: the Python default wins and the answer is wrong at 200."""

    def dependency(
        request: Request, size: Annotated[int, Query(maximum=10)] = 5
    ) -> int:  # pragma: no cover
        return size

    app = wreath.Wreath()

    @app.get("/a")
    async def handler(request: Request, value: int = Depends(dependency)) -> dict:
        return {}  # pragma: no cover

    with pytest.raises(TypeError, match="a parameter wreath never binds"):
        app._compile_routes()


def test_a_plain_dependency_default_is_untouched() -> None:
    """Over-refusal guard: an ordinary default stays an ordinary default."""

    def dependency(request: Request, scale: int = 3) -> int:
        return scale

    app = wreath.Wreath()

    @app.get("/a")
    async def handler(request: Request, value: int = Depends(dependency)) -> dict:
        return {"value": value}

    app._compile_routes()


# --- page_params: the shipped dependency the defect made unusable -------------


@pytest.mark.asyncio
async def test_page_params_binds_the_query_string() -> None:
    """What `wreath.pagination` has always documented it does, now true.

    It reads `request.query_string` itself rather than carrying `Query()`
    markers, because a dependency's own parameters are never bound.
    """
    app = wreath.Wreath()

    @app.get("/llamas")
    async def listing(request: Request, params: PageParams = Depends(page_params)) -> dict:
        return {"page": params.page, "size": params.size, "sort": list(params.sort)}

    async with TestClient(app) as client:
        assert (await client.get("/llamas")).json() == {
            "page": 1,
            "size": DEFAULT_SIZE,
            "sort": [],
        }
        bound = await client.get("/llamas?page=3&size=5&sort=name,-created_at")
        assert bound.status == 200
        assert bound.json() == {"page": 3, "size": 5, "sort": ["name", "-created_at"]}


@pytest.mark.asyncio
async def test_page_params_clamps_rather_than_refusing() -> None:
    """A page past the end asks for the last page; that is not a protocol error.

    `MAX_PAGE` exists because `LIMIT/OFFSET` makes the database walk and discard
    every row before the offset, so an unbounded page number is a scan an
    anonymous caller could ask for repeatedly.
    """
    app = wreath.Wreath()

    @app.get("/llamas")
    async def listing(request: Request, params: PageParams = Depends(page_params)) -> dict:
        return {"page": params.page, "size": params.size}

    async with TestClient(app) as client:
        assert (await client.get("/llamas?page=999999&size=9999")).json() == {
            "page": MAX_PAGE,
            "size": MAX_SIZE,
        }
        assert (await client.get("/llamas?page=abc&size=")).json() == {
            "page": 1,
            "size": DEFAULT_SIZE,
        }


# --- identify(): anonymous is a value, not an error ---------------------------


class _HeaderBackend(AuthenticationBackend):
    """Identifies whoever names themselves in `x-who`, and nobody otherwise."""

    async def authenticate(self, request: Any) -> Identity | None:
        who = request.header("x-who")
        return Identity(id=who) if who else None

    def challenge(self, request: Any) -> str:
        return "Header"


def _identity_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.configure_auth(backend=_HeaderBackend())

    @app.get("/session")
    @identify()
    async def whoami(request: Request) -> dict:
        found = request.identity
        return {"signed_in": found is not None, "id": None if found is None else found.id}

    @app.get("/private")
    @authenticated()
    async def private(request: Request) -> dict:
        return {"ok": True}

    @app.get("/public")
    async def public(request: Request) -> dict:
        return {"identity_is": "none" if request.identity is None else "set"}

    return app


@pytest.mark.asyncio
async def test_identify_publishes_the_identity_it_finds() -> None:
    """The defect: a caller holding a good session was told `signed_in: false`.

    `request.identity` is populated only when the backend runs, and the backend
    ran only for a route that *required* an identity -- so the one question a
    sign-in-aware page asks on load could not be answered.
    """
    async with TestClient(_identity_app()) as client:
        known = await client.get("/session", headers={"x-who": "bo"})
        assert known.status == 200
        assert known.json() == {"signed_in": True, "id": "bo"}


@pytest.mark.asyncio
async def test_identify_admits_an_anonymous_caller() -> None:
    """No challenge, no 401 -- the route is public and the answer is `None`."""
    async with TestClient(_identity_app()) as client:
        anonymous = await client.get("/session")
        assert anonymous.status == 200
        assert anonymous.json() == {"signed_in": False, "id": None}


@pytest.mark.asyncio
async def test_authenticated_still_refuses_an_anonymous_caller() -> None:
    """The distinction is the point: `identify` must not soften `authenticated`."""
    async with TestClient(_identity_app()) as client:
        assert (await client.get("/private")).status == 401
        assert (await client.get("/private", headers={"x-who": "bo"})).status == 200


@pytest.mark.asyncio
async def test_a_route_with_no_requirement_still_sees_no_identity() -> None:
    """The control. Without it, `identify` publishing an identity proves nothing.

    An unmarked route must stay unmarked: the backend is not asked, so a caller
    holding a perfectly good credential is still anonymous there. That is the
    documented contract and the reason `identify()` has to exist at all.
    """
    async with TestClient(_identity_app()) as client:
        response = await client.get("/public", headers={"x-who": "bo"})
        assert response.json() == {"identity_is": "none"}
