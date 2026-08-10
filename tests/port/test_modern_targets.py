"""Constructs wreath grew an answer for after the rule catalog was written.

A porting tool ages badly in one specific way: a construct is catalogued as
"unsupported — keep the library", wreath later ships the thing, and the report
goes on telling porters to keep a dependency they could now delete. Every rule
here was chosen by counting occurrences in a real production FastAPI/ormar
codebase, so the catalog spends its attention where the work actually is.

Ordered by how much of an application each accounts for: alembic operations
~1400, cachetools ~170, arrow ~110, strawberry ~450, `Body(...)` 80, HTTP status
constants 72, `httpx.AsyncClient` 70, `dependency_overrides` 87, FastAPI's
`TestClient` 63.
"""
import pytest

port = pytest.importorskip("wreath.port")


def _analyze(tmp_path, source: str, name: str = "module.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return port.analyze(path).findings


def _rule_ids(tmp_path, source: str, name: str = "module.py") -> list[str]:
    return [f.rule_id for f in _analyze(tmp_path, source, name)]


def _message(tmp_path, source: str, rule_id: str) -> str:
    for finding in _analyze(tmp_path, source):
        if finding.rule_id == rule_id:
            return finding.message
    raise AssertionError(f"{rule_id} was not reported for:\n{source}")


# --- caching: the payoff of ORM-driven invalidation --------------------------------


CACHETOOLS = """
from cachetools import TTLCache, cached

_herd = TTLCache(maxsize=512, ttl=300)


@cached(_herd)
def herd_summary(paddock_id):
    return paddock_id
"""


def test_a_cachetools_store_is_pointed_at_the_wreath_cache(tmp_path) -> None:
    assert "cache.store" in _rule_ids(tmp_path, CACHETOOLS)


def test_a_cachetools_decorator_is_pointed_at_invalidate_on(tmp_path) -> None:
    """The upgrade, not just the equivalent.

    A `TTLCache(ttl=300)` is a guess about how stale the data may get. Wreath's
    `@cached(invalidate_on=[...])` clears on the committed write instead, so the
    message has to name that rather than offering a like-for-like TTL.
    """
    message = _message(tmp_path, CACHETOOLS, "cache.decorator")
    assert "invalidate_on" in message


def test_an_lru_cache_is_recognised_too(tmp_path) -> None:
    source = "from cachetools import LRUCache\n_c = LRUCache(maxsize=128)\n"
    assert "cache.store" in _rule_ids(tmp_path, source)


def test_a_module_reports_its_cache_stores_once_each(tmp_path) -> None:
    """Two stores are two decisions; ten uses of one store are not ten."""
    source = (
        "from cachetools import TTLCache\n"
        "a = TTLCache(maxsize=1, ttl=1)\n"
        "b = TTLCache(maxsize=2, ttl=2)\n"
    )
    assert _rule_ids(tmp_path, source).count("cache.store") == 2


def test_the_functools_lru_cache_is_left_alone(tmp_path) -> None:
    """Stdlib memoization is not a framework cache and needs no porting."""
    source = "from functools import lru_cache\n\n@lru_cache\ndef f(x):\n    return x\n"
    assert "cache.store" not in _rule_ids(tmp_path, source)
    assert "cache.decorator" not in _rule_ids(tmp_path, source)


# --- time -------------------------------------------------------------------------


def test_arrow_is_reported_once_per_module(tmp_path) -> None:
    source = (
        "import arrow\n"
        "a = arrow.utcnow()\n"
        "b = arrow.get('2026-07-26')\n"
        "c = arrow.now()\n"
    )
    assert _rule_ids(tmp_path, source).count("time.arrow") == 1


def test_the_arrow_message_names_temporal_now_that_it_ships(tmp_path) -> None:
    """This rule used to say a native temporal layer was designed, not shipped.

    It shipped. Leaving the old wording in place would have told porters to
    hand-roll the stdlib equivalent of code wreath now owns — which is the exact
    way a porting tool goes stale, and the reason the catalog is re-audited when
    a subsystem lands.
    """
    message = _message(tmp_path, "import arrow\nx = arrow.utcnow()\n", "time.arrow")
    assert "temporal" in message
    assert "designed" not in message


@pytest.mark.parametrize(
    "call", ["arrow.utcnow()", "arrow.now()", "arrow.get('2026-07-26')"]
)
def test_an_arrow_rename_is_translated(tmp_path, call) -> None:
    source = f"import arrow\nx = {call}\n"
    (finding,) = [f for f in _analyze(tmp_path, source) if f.rule_id == "time.arrow"]
    assert finding.tag == port.TRANSLATED


def test_an_arrow_calendar_shift_still_needs_a_decision(tmp_path) -> None:
    """`shift(months=)` is not a fixed number of seconds, and temporal says so.

    The split is per-constructor rather than per-module precisely so this does
    not ride along on the translated verdict the clock calls earn.
    """
    source = "import arrow\nx = arrow.Arrow.fromdate(d).shift(months=6)\n"
    ids = _rule_ids(tmp_path, source)
    assert "time.arrow_other" in ids
    assert "time.arrow" not in ids


# --- GraphQL: a rule that had gone stale ------------------------------------------


STRAWBERRY = """
import strawberry


@strawberry.type
class Llama:
    id: strawberry.auto
    name: strawberry.auto

    @strawberry.field
    def trek_count(self) -> int:
        return 0


@strawberry.input
class LlamaFilter:
    paddock_id: strawberry.auto
"""


def test_a_strawberry_type_points_at_wreath_graphql(tmp_path) -> None:
    assert _rule_ids(tmp_path, STRAWBERRY).count("graphql.type") == 2


def test_a_strawberry_resolver_is_reported_separately(tmp_path) -> None:
    """A computed field is real logic to port; an `auto` field is not."""
    assert "graphql.resolver" in _rule_ids(tmp_path, STRAWBERRY)


def test_auto_fields_do_not_each_become_a_finding(tmp_path) -> None:
    """`strawberry.auto` is the single most common GraphQL token there is.

    Wreath derives fields from the ORM model, so every one of them is deleted
    rather than ported. Billing 305 findings for work that is a no-op would bury
    the two findings that matter.
    """
    assert "graphql.auto_field" not in _rule_ids(tmp_path, STRAWBERRY)


def test_a_graphql_server_is_no_longer_unsupported(tmp_path) -> None:
    """`wreath.graphql` shipped; the catalog said "no equivalent" for too long."""
    source = "from strawberry.fastapi import GraphQLRouter\nr = GraphQLRouter(schema)\n"
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id == "graphql.mount"]
    assert findings
    assert all(f.tag == port.NEEDS_REVIEW for f in findings)
    assert all("GraphQL" in f.message for f in findings)


# --- outbound HTTP ------------------------------------------------------------------


def test_an_httpx_client_points_at_the_managed_pool(tmp_path) -> None:
    source = (
        "import httpx\n"
        "async def fetch():\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        return await client.get('/treks')\n"
    )
    message = _message(tmp_path, source, "ext.httpx")
    assert "http_client" in message
    assert "ServiceClient" in message
    assert "compatibility layer" in message


def test_httpx_is_reported_once_per_module(tmp_path) -> None:
    source = (
        "import httpx\n"
        "a = httpx.AsyncClient()\n"
        "b = httpx.AsyncClient()\n"
    )
    assert _rule_ids(tmp_path, source).count("ext.httpx") == 1


# --- migrations -----------------------------------------------------------------------


def test_a_schema_operation_points_at_wreath_migrations(tmp_path) -> None:
    """Ordinary DDL over modelled objects has no wreath counterpart to hand-write.

    Wreath's migration source of truth is the ORM image: `detect` reads the
    change off the models and `generate` emits the artifact. So the determined
    target for these two lines is *no code* — the same shape of answer as
    `extra="forbid"` (drop it) or `jsonable_encoder` (drop it), both of which the
    catalog already calls translated.
    """
    source = (
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "def upgrade():\n"
        "    op.add_column('llama', sa.Column('nickname', sa.String()))\n"
        "    op.create_index('ix_llama_paddock', 'llama', ['paddock_id'])\n"
    )
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id == "mig.derived"]
    assert len(findings) == 2
    assert all(f.tag == port.TRANSLATED for f in findings)
    assert all("nothing to write" in f.message for f in findings)


@pytest.mark.parametrize(
    ("call", "expected", "why"),
    [
        ("op.rename_table('paddock', 'pasture')", "mig.rename",
         "an image differ reads a rename as drop+create, which moves no data"),
        ("op.alter_column('llama', 'grade', new_column_name='band')", "mig.rename",
         "same hazard, spelled as a column rename"),
        ("op.create_index('i', 'llama', ['name'], postgresql_where=sa.text('grade > 3'))",
         "mig.index_manual", "a partial index is emitted as a MANUAL op"),
        ("op.create_index('i', 'llama', [sa.text('lower(name)')])", "mig.index_manual",
         "an expression index is not btree-over-columns"),
        ("op.create_index('i', 'llama', columns)", "mig.index_manual",
         "a runtime column list is not readable here"),
        ("op.add_column('llama', sa.Column('shorn_at', sa.Time()))",
         "mig.unmodelled_type", "wreath.orm.types has no Time PgType"),
        ("op.create_table('t', sa.Column('kind', sa.Enum('a', 'b')))", "mig.unmodelled_type",
         "nor an Enum type"),
        ("op.create_check_constraint('c', 'llama', 'grade > 0')", "mig.schema_op",
         "a check constraint is not in what detection reads"),
        ("op.drop_constraint('uq_llama_name', 'llama')", "mig.schema_op",
         "the call does not say which constraint kind, so the model attribute is unknown"),
        ("op.create_table('t', sa.Column('r', sa.Integer(), "
         "sa.ForeignKey('o.id', ondelete='CASCADE')))", "mig.schema_op",
         "a referential action belongs to the constraint, not to a modelled column"),
        ("op.alter_column('llama', 'name', comment='the name')", "mig.schema_op",
         "wreath does not model column comments"),
    ],
)
def test_a_migration_operation_outside_what_detect_reads_is_not_derived(
    tmp_path, call, expected, why
) -> None:
    """The honesty half of the Alembic verdict.

    Every one of these *looks* like ordinary DDL. The rename pair is the one worth
    keeping: it is the only ordinary-looking operation whose derived form is
    actively wrong rather than merely absent.
    """
    source = f"from alembic import op\nimport sqlalchemy as sa\ndef upgrade():\n    {call}\n"
    ids = _rule_ids(tmp_path, source)
    assert expected in ids, f"{call}: {why}"
    assert "mig.derived" not in ids, f"{call}: {why}"


def test_a_plain_foreign_key_and_primary_key_still_derive(tmp_path) -> None:
    """Detection covers primary keys and foreign keys, so these stay derivable."""
    source = (
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "def upgrade():\n"
        "    op.create_table('trek', sa.Column('id', sa.Integer()),\n"
        "                    sa.Column('llama_id', sa.Integer(), sa.ForeignKey('llama.id')),\n"
        "                    sa.PrimaryKeyConstraint('id'))\n"
    )
    assert "mig.derived" in _rule_ids(tmp_path, source)


def test_raw_sql_in_a_migration_stays_manual(tmp_path) -> None:
    source = "from alembic import op\ndef upgrade():\n    op.execute('ANALYZE llama')\n"
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id == "mig.raw_sql"]
    assert findings and all(f.tag == port.UNSUPPORTED for f in findings)


def test_a_data_migration_is_called_out_as_its_own_hazard(tmp_path) -> None:
    """`op.get_bind()` means the migration rewrites rows, not just the schema.

    That is the operation that takes an hour on a large table and blocks a
    deploy, so it deserves naming rather than being counted as one more DDL op.
    """
    source = (
        "from alembic import op\n"
        "def upgrade():\n"
        "    conn = op.get_bind()\n"
        "    conn.execute('UPDATE llama SET grade = 1')\n"
    )
    assert "mig.data" in _rule_ids(tmp_path, source)


def test_a_variable_named_op_is_not_mistaken_for_alembic(tmp_path) -> None:
    """Name resolution, not string matching: `op` is a common local name."""
    source = "class Thing:\n    pass\nop = Thing()\nop.add_column('x')\n"
    assert "mig.schema_op" not in _rule_ids(tmp_path, source)


# --- responses and status ---------------------------------------------------------------


def test_a_status_constant_translates(tmp_path) -> None:
    source = (
        "from fastapi import status\n"
        "def f():\n"
        "    return status.HTTP_404_NOT_FOUND\n"
    )
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id == "resp.status_const"]
    assert findings and all(f.tag == port.TRANSLATED for f in findings)


def test_a_status_constant_raise_is_a_literal_exception(tmp_path) -> None:
    """`status.HTTP_404_NOT_FOUND` is a literal wearing a name.

    It is how a real codebase spells the status far more often than a bare
    integer, so treating it as unresolvable would push the majority of
    `HTTPException` sites into needs-review for no reason.
    """
    source = (
        "from fastapi import HTTPException, status\n"
        "def f():\n"
        "    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no')\n"
    )
    ids = _rule_ids(tmp_path, source)
    assert "exc.http_literal" in ids and "exc.http_variable" not in ids


def test_a_genuinely_variable_status_still_needs_review(tmp_path) -> None:
    source = (
        "from fastapi import HTTPException\n"
        "def f(code):\n"
        "    raise HTTPException(status_code=code)\n"
    )
    assert "exc.http_variable" in _rule_ids(tmp_path, source)


def test_a_response_class_translates(tmp_path) -> None:
    source = (
        "from fastapi.responses import JSONResponse, StreamingResponse\n"
        "a = JSONResponse({'ok': True})\n"
        "b = StreamingResponse(iter(()))\n"
    )
    ids = _rule_ids(tmp_path, source)
    assert ids.count("resp.class") == 2


def test_jsonable_encoder_translates_to_nothing(tmp_path) -> None:
    """Wreath serializes dataclasses and ORM rows directly, so the call is dropped."""
    source = (
        "from fastapi.encoders import jsonable_encoder\n"
        "x = jsonable_encoder({'a': 1})\n"
    )
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id == "resp.jsonable"]
    assert findings and all(f.tag == port.TRANSLATED for f in findings)


def test_a_plain_body_marker_is_an_ordinary_body_param(tmp_path) -> None:
    source = (
        "from fastapi import APIRouter, Body\n"
        "router = APIRouter()\n"
        "@router.post('/llamas')\n"
        "async def create(name: str = Body(...)):\n"
        "    return name\n"
    )
    assert "param.body" in _rule_ids(tmp_path, source)


def test_an_embedded_body_marker_needs_review(tmp_path) -> None:
    """`embed=True` changes the wire shape, so it is not a 1:1 port."""
    source = (
        "from fastapi import APIRouter, Body\n"
        "router = APIRouter()\n"
        "@router.patch('/llamas/{llama_id}')\n"
        "async def regrade(llama_id: str, grade: int = Body(..., embed=True)):\n"
        "    return grade\n"
    )
    ids = _rule_ids(tmp_path, source)
    assert "param.body_embed" in ids and "param.body" not in ids


def test_a_response_class_route_option_needs_review(tmp_path) -> None:
    source = (
        "from fastapi import APIRouter\n"
        "from fastapi.responses import HTMLResponse\n"
        "router = APIRouter()\n"
        "@router.get('/herd', response_class=HTMLResponse)\n"
        "async def herd():\n"
        "    return '<p>Bea</p>'\n"
    )
    assert "route.response_class" in _rule_ids(tmp_path, source)


# --- auth schemes --------------------------------------------------------------------


@pytest.mark.parametrize(
    "scheme",
    ["HTTPBearer", "HTTPBasic", "APIKeyHeader", "OAuth2PasswordBearer"],
)
def test_a_declarative_security_scheme_points_at_a_backend(tmp_path, scheme) -> None:
    source = f"from fastapi.security import {scheme}\nscheme = {scheme}()\n"
    assert "auth.security_scheme" in _rule_ids(tmp_path, source)


def test_security_wraps_a_dependency(tmp_path) -> None:
    """`Security(...)` is `Depends(...)` plus scopes; wreath has no scope slot."""
    source = (
        "from fastapi import Security\n"
        "from fastapi.security import HTTPBearer\n"
        "scheme = HTTPBearer()\n"
        "def dep(cred = Security(scheme)):\n"
        "    return cred\n"
    )
    assert "auth.security" in _rule_ids(tmp_path, source)


# --- the test suite itself ---------------------------------------------------------


def test_a_fastapi_test_client_is_reported(tmp_path) -> None:
    """Wreath's client is async, so the rewrite is real work, not a rename."""
    source = (
        "from fastapi.testclient import TestClient\n"
        "client = TestClient(app)\n"
    )
    message = _message(tmp_path, source, "test.client")
    assert "async with" in message


def test_a_dependency_override_points_at_acting_as(tmp_path) -> None:
    """Most overrides swap the auth dependency, and wreath has a way to do that."""
    source = "app.dependency_overrides[authenticate] = lambda: rider\n"
    message = _message(tmp_path, source, "test.dependency_override")
    assert "acting_as" in message
    assert "ServiceClient" in message
    assert "same Session" in message
    assert "Delete fake repositories" in message
    assert "app.state" not in message


# --- libraries to keep -----------------------------------------------------------


def test_pandas_is_reported_once_and_left_alone(tmp_path) -> None:
    """198 files use it. It is not a framework feature and wreath will not try."""
    source = (
        "import pandas as pd\n"
        "a = pd.DataFrame()\n"
        "b = pd.DataFrame()\n"
    )
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id == "ext.pandas"]
    assert len(findings) == 1
    assert findings[0].tag == port.UNSUPPORTED


# --- the catalog itself -------------------------------------------------------------


def test_every_rule_is_reachable_by_id() -> None:
    """A rule nobody can emit is dead weight the report never explains."""
    from wreath._port.rules import RULES

    assert all(isinstance(v, tuple) and len(v) == 4 for v in RULES.values())


def test_every_rule_has_a_valid_tag_and_category() -> None:
    from wreath._port.ir import VALID_TAGS
    from wreath._port.rules import RULES

    for rule_id, (construct, category, tag, message) in RULES.items():
        assert tag in VALID_TAGS, rule_id
        assert construct and category and message, rule_id
