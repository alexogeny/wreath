"""`--opinionated`: make the decision instead of writing it down.

The default emit stops at the file boundary. A query needs a session to run, a
session has to come from somewhere, and "somewhere" is the function's caller —
so changing it changes code the emitter is not looking at. That is a decision
about someone else's code, and a codemod should be asked before it makes one.

`--opinionated` is that permission. It threads the session from the route
handler, where wreath supplies one, down through every function that needs it,
and updates the calls on the way — so the queries are written out rather than
described.

**The signature and the call sites move together or not at all.** Adding the
parameter and leaving the callers is the half-port that imports cleanly and
fails on the first request, which is worse than the note it replaced.
"""
from __future__ import annotations

import ast

import pytest

port = pytest.importorskip("wreath.port")

_REPOSITORY = (
    "class LlamaRepository:\n"
    "    async def llamas_in(self, herd):\n"
    "        return await Llama.objects.filter(herd=herd).all()\n"
)
_SERVICE = (
    "from repo import LlamaRepository\n\n\n"
    "async def summarise_herd(herd):\n"
    "    return len(await LlamaRepository().llamas_in(herd))\n"
)
_ROUTER = (
    "from fastapi import APIRouter\n"
    "from service import summarise_herd\n\n"
    "router = APIRouter()\n\n\n"
    '@router.get("/summary/{herd}")\n'
    "async def summary(herd: str):\n"
    '    return {"n": await summarise_herd(herd)}\n'
)


@pytest.fixture
def app_tree(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "repo.py").write_text(_REPOSITORY, encoding="utf-8")
    (root / "service.py").write_text(_SERVICE, encoding="utf-8")
    (root / "router.py").write_text(_ROUTER, encoding="utf-8")
    return root


def _port(root, out, **kwargs) -> dict[str, str]:
    port.port_tree(root, out, **kwargs)
    ported = {}
    for path in sorted(out.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec", dont_inherit=True)
        ported[path.name] = text
    return ported


def test_by_default_a_query_outside_a_handler_is_described_not_moved(app_tree, tmp_path):
    """One note on the function, not one on every query inside it."""
    ported = _port(app_tree, tmp_path / "out")
    assert "orm.query.needs_session" in ported["repo.py"]
    assert "Llama.objects.filter" in ported["repo.py"]     # left exactly as written
    assert "session" not in ported["service.py"].replace("session", "", 0).split("\n")[0]
    assert "Session" not in ported["service.py"]


def test_opinionated_threads_the_session_from_the_handler_down(app_tree, tmp_path):
    ported = _port(app_tree, tmp_path / "out", opinionated=True)

    # the handler asks wreath for one
    assert "session: Annotated[Session, FromORM()]" in ported["router.py"]
    # ... and passes it on
    assert "summarise_herd(herd, session=session)" in ported["router.py"]
    # the middle of the chain takes one and passes it on, without running a query itself
    assert "session: Session" in ported["service.py"]
    assert "llamas_in(herd, session=session)" in ported["service.py"]
    # and the query is written out rather than described
    assert "await session.fetch(Llama.select().where(Llama.herd == herd))" in ported["repo.py"]
    assert "objects" not in ported["repo.py"]


def test_a_handler_that_already_owns_a_wreath_session_reuses_it(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "router.py").write_text(
        "from fastapi import APIRouter\n"
        "from wreath.orm import Session\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/llamas")\n'
        "async def llamas(session: Session):\n"
        "    return await Llama.objects.all()\n",
        encoding="utf-8",
    )

    source = _port(root, tmp_path / "out", opinionated=True)["router.py"]

    assert source.count("session: Session") == 1
    assert "await session.fetch(Llama.select())" in source
    assert "orm.query.needs_session" not in source


def test_required_create_and_bounded_writes_use_session_contracts(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "queries.py").write_text(
        "async def work(llama_id, name):\n"
        "    row = await Llama.objects.get(id=llama_id)\n"
        "    made = await Llama.objects.create(name=name)\n"
        "    changed = await Llama.objects.filter(id=llama_id).update(name=name)\n"
        "    removed = await Llama.objects.filter(id=llama_id).delete()\n"
        "    return row, made, changed, removed\n",
        encoding="utf-8",
    )

    source = _port(root, tmp_path / "out", opinionated=True)["queries.py"]

    assert "await session.require(Llama, llama_id)" in source
    assert "await session.create(Llama, name=name)" in source
    assert (
        "await session.update_where(Llama.select().where(Llama.id == llama_id), name=name)"
        in source
    )
    assert "await session.delete_where(Llama.select().where(Llama.id == llama_id))" in source


def test_a_local_test_client_becomes_one_async_lifespan(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "test_llamas.py").write_text(
        "from fastapi.testclient import TestClient\n\n"
        "def test_list():\n"
        "    client = TestClient(app)\n"
        "    response = client.get('/llamas')\n"
        "    assert response.status_code == 200\n",
        encoding="utf-8",
    )

    source = _port(root, tmp_path / "out", opinionated=True)["test_llamas.py"]

    assert "async def test_list():" in source
    assert "async with TestClient(app) as client:" in source
    assert "response = await client.get('/llamas')" in source
    assert "response.status == 200" in source


def test_a_resolved_relationship_filter_uses_the_wreath_path(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "models.py").write_text(
        "import ormar\n"
        "class Ranch(ormar.Model):\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    slug: str = ormar.String(max_length=40)\n"
        "class Llama(ormar.Model):\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ranch: Ranch = ormar.ForeignKey(Ranch)\n",
        encoding="utf-8",
    )
    (root / "queries.py").write_text(
        "async def by_ranch():\n"
        "    return await Llama.objects.filter(ranch__slug='north').all()\n",
        encoding="utf-8",
    )

    source = _port(root, tmp_path / "out", opinionated=True)["queries.py"]

    assert "Llama.ranch.slug == 'north'" in source


def test_a_plain_runtime_mapping_uses_where_fields_not_a_manager(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "queries.py").write_text(
        "async def search(name):\n"
        "    terms = {}\n"
        "    if name:\n"
        "        terms['name'] = name\n"
        "    return await Llama.objects.filter(**terms).all()\n",
        encoding="utf-8",
    )

    source = _port(root, tmp_path / "out", opinionated=True)["queries.py"]

    assert "*where_fields(Llama, terms)" in source
    assert "from wreath.orm import" in source and "where_fields" in source


def test_a_plain_graphql_output_loses_the_strawberry_runtime_model(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "schema.py").write_text(
        "import strawberry\n"
        "@strawberry.type\n"
        "class TrekSummary:\n"
        "    label: str\n"
        "    count: int\n",
        encoding="utf-8",
    )

    source = _port(root, tmp_path / "out", opinionated=True)["schema.py"]

    assert "@dataclass(kw_only=True)" in source
    assert "@strawberry.type" not in source
    assert "import strawberry" not in source
    assert "graphql.type_dataclass" in source


def test_every_ported_file_imports_what_it_uses(app_tree, tmp_path):
    """The Session annotation is worth nothing without the import beside it."""
    ported = _port(app_tree, tmp_path / "out", opinionated=True)
    for name, text in ported.items():
        if "Session" in text:
            assert "from wreath.orm import" in text and "Session" in text, name


def test_a_name_that_could_mean_something_else_is_left_alone(tmp_path):
    """A repository with an `async def all` must not teach the tool to rewrite `all()`.

    This is the risk in matching a method by name, and it bites: a repository
    with an `async def all(self)` is ordinary, and every built-in `all(...)` in
    that tree was handed a session.
    """
    root = tmp_path / "app"
    root.mkdir()
    (root / "repo.py").write_text(
        "class LlamaRepository:\n"
        "    async def all(self):\n"
        "        return await Llama.objects.all()\n",
        encoding="utf-8",
    )
    (root / "check.py").write_text(
        "async def every_llama_named(llamas):\n"
        "    return all(llama.name for llama in llamas)\n",
        encoding="utf-8",
    )
    ported = _port(root, tmp_path / "out", opinionated=True)
    assert "all(llama.name for llama in llamas)" in ported["check.py"]
    assert "session" not in ported["check.py"]


def test_a_generator_argument_gains_its_brackets_with_the_keyword(tmp_path):
    """`f(x for x in y)` may go bare only while it is the only argument."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "repo.py").write_text(
        "async def llamas_for(herd):\n"
        "    return await Llama.objects.filter(herd=herd).all()\n\n\n"
        "async def herd_sizes(herds):\n"
        "    return sum(len(await llamas_for(h)) for h in herds)\n",
        encoding="utf-8",
    )
    ported = _port(root, tmp_path / "out", opinionated=True)
    tree = ast.parse(ported["repo.py"])        # compiled by `_port` already
    assert tree is not None
    assert "session=session" in ported["repo.py"]


def test_a_call_with_a_trailing_comma_does_not_get_two(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "repo.py").write_text(
        "async def llamas_for(herd, ranch):\n"
        "    return await Llama.objects.filter(herd=herd, ranch=ranch).all()\n\n\n"
        "async def summarise(herd):\n"
        "    return await llamas_for(\n"
        "        herd,\n"
        '        "north",\n'
        "    )\n",
        encoding="utf-8",
    )
    ported = _port(root, tmp_path / "out", opinionated=True)
    assert ",," not in ported["repo.py"].replace(" ", "")


def test_opinionated_drops_extra_ignore_instead_of_asking(tmp_path):
    """Wreath always rejects unknown fields, so there is nothing to decide."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "dto.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class Llama(BaseModel):\n"
        '    model_config = ConfigDict(extra="ignore")\n'
        "    name: str\n",
        encoding="utf-8",
    )
    default = _port(root, tmp_path / "a")["dto.py"]
    opinionated = _port(root, tmp_path / "b", opinionated=True)["dto.py"]
    assert "pydantic.config_ignore" in default
    assert "pydantic.config_ignore" not in opinionated
    assert "extra='ignore' dropped" in opinionated
