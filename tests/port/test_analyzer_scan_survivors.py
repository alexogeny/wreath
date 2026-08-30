from __future__ import annotations

import ast
from pathlib import Path

import pytest

import wreath._port.analyzer.scan as analyzer_scan
from wreath import port
from wreath._port.analyzer.django import DjangoImage
from wreath._port.analyzer.imports import _Imports
from wreath._port.analyzer.scan import _Analyzer


def _findings(tmp_path: Path, source: str) -> list[tuple[int, str]]:
    module = tmp_path / "app.py"
    module.write_text(source, encoding="utf-8")
    return [(finding.line, finding.rule_id) for finding in port.analyze(module).findings]


def _direct_findings(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    analyzer = _Analyzer(
        Path("app.py"),
        Path("."),
        _Imports().visit(tree),
        {"settings": set(), "pydantic": set(), "orm": set()},
    )
    analyzer.visit(tree)
    return [(finding.line, finding.rule_id) for finding in analyzer.findings]


def test_constructor_defaults_and_supplied_indexes_are_preserved() -> None:
    imports = _Imports()
    supplied_django = DjangoImage()
    supplied_pk_types = {"User": "uuid"}
    supplied_columns = {"User": {"id"}}
    supplied_relations = {"User": {"owner": "Account"}}
    supplied_tables = {"User": "users"}
    supplied_constraints = {"User": (frozenset({"email"}),)}
    analyzer = _Analyzer(
        Path("app.py"),
        Path("."),
        imports,
        {"settings": set(), "pydantic": set(), "orm": set()},
        pk_types=supplied_pk_types,
        orm_columns=supplied_columns,
        orm_relations=supplied_relations,
        orm_tables=supplied_tables,
        orm_unique_constraints=supplied_constraints,
        django=supplied_django,
    )

    assert analyzer.django is supplied_django
    assert analyzer.pk_types is supplied_pk_types
    assert analyzer.orm_columns is supplied_columns
    assert analyzer.orm_relations is supplied_relations
    assert analyzer.orm_tables is supplied_tables
    assert analyzer.orm_unique_constraints is supplied_constraints

    defaults = _Analyzer(
        Path("app.py"),
        Path("."),
        imports,
        {"settings": set(), "pydantic": set(), "orm": set()},
    )
    assert isinstance(defaults.django, DjangoImage)
    assert defaults.pk_types == {}
    assert defaults.orm_columns == {}
    assert defaults.orm_relations == {}
    assert defaults.orm_tables == {}
    assert defaults.orm_unique_constraints == {}


def test_class_shapes_keep_distinct_verdicts(tmp_path: Path) -> None:
    source = '''from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
import strawberry

@strawberry.type()
class Output:
    value: strawberry.auto

@strawberry.input
class Input:
    value: strawberry.auto

class Positional(BaseModel):
    optional: str = ""
    required: int

Positional("", 1)

class Configured(BaseModel):
    class Config:
        extra = "allow"
    name: str
    model_config = ConfigDict(extra="ignore")

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
'''

    assert _findings(tmp_path, source) == [
        (6, "graphql.type"),
        (10, "graphql.type"),
        (13, "pydantic.model"),
        (13, "pydantic.model_kw_only"),
        (14, "pydantic.field"),
        (15, "pydantic.field"),
        (19, "pydantic.model"),
        (20, "pydantic.config_class"),
        (22, "pydantic.field"),
        (23, "pydantic.config_ignore"),
        (25, "pydantic.model"),
        (26, "pydantic.config_forbid"),
        (27, "pydantic.field"),
    ]


def test_class_decorators_and_settings_messages_keep_their_exact_shape() -> None:
    source = '''from fastapi import Form
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings

@Form.as_form()
class FormModel(BaseModel):
    value: str

class Nested(BaseModel):
    class Other:
        pass
    self.value: str
    model_config: object
    model_config = other = ConfigDict(extra="allow")
    other_config = ConfigDict(extra="ignore")
    holder.other = model_config = ConfigDict(extra="ignore")

class Environment(BaseSettings):
    required: str

class ComplexEnvironment(BaseSettings):
    values: list[str]
'''
    findings = _direct_findings(source)

    assert findings == [
        (6, "form.as_form"),
        (6, "pydantic.model"),
        (7, "pydantic.field"),
        (9, "pydantic.model"),
        (16, "pydantic.config_ignore"),
        (19, "settings.field"),
        (18, "settings.class_env"),
        (22, "settings.field_complex"),
        (21, "settings.class"),
    ]

    tree = ast.parse(source)
    analyzer = _Analyzer(
        Path("app.py"),
        Path("."),
        _Imports().visit(tree),
        {"settings": set(), "pydantic": set(), "orm": set()},
    )
    analyzer.visit(tree)
    environment = next(f for f in analyzer.findings if f.rule_id == "settings.class_env")
    assert environment.message.endswith("(required_env=[REQUIRED])")
    complex_environment = next(f for f in analyzer.findings if f.rule_id == "settings.class")
    assert "required_env" not in complex_environment.message


def test_graphql_mirror_requires_exact_auto_model_fields() -> None:
    tree = ast.parse(
        '''import strawberry
class Exact:
    value: strawberry.auto
class Empty:
    pass
class Declared:
    value: str
class AttributeTarget:
    self.value: strawberry.auto
class Method:
    value: strawberry.auto
    def resolve(self):
        return self.value
class Mixed:
    automatic: strawberry.auto
    declared: str
'''
    )
    imports = _Imports().visit(tree)
    analyzer = _Analyzer(
        Path("app.py"),
        Path("."),
        imports,
        {"settings": set(), "pydantic": set(), "orm": set()},
        orm_columns={"Exact": {"value"}},
    )
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]

    assert [analyzer._graphql_type_shape(node, "type") for node in classes] == [
        ("graphql.type_mirror", ""),
        (
            "graphql.type",
            "some fields are declared rather than `strawberry.auto`, so they are not "
            "provably the model's columns",
        ),
        ("graphql.type_dataclass", "register it in GraphQL(dataclasses=[...])"),
        (
            "graphql.type",
            "some fields are declared rather than `strawberry.auto`, so they are not "
            "provably the model's columns",
        ),
        ("graphql.type", "the class carries resolvers, so it is not just a mirror"),
        (
            "graphql.type",
            "some fields are declared rather than `strawberry.auto`, so they are not "
            "provably the model's columns",
        ),
    ]


def test_foreign_key_attribute_targets_use_the_indexed_primary_key_type() -> None:
    tree = ast.parse('''import ormar
class Item(ormar.Model):
    owner = ormar.ForeignKey(models.User)
''')
    analyzer = _Analyzer(
        Path("app.py"),
        Path("."),
        _Imports().visit(tree),
        {"settings": set(), "pydantic": set(), "orm": set()},
        pk_types={"User": "Uuid"},
    )
    analyzer.visit(tree)

    assert [(finding.line, finding.rule_id) for finding in analyzer.findings] == [
        (2, "orm.model"),
        (3, "orm.fk_typed"),
    ]


def test_orm_fields_require_field_calls_and_supported_origins(tmp_path: Path) -> None:
    source = '''import ormar
from sqlmodel import SQLModel, Field

class OrmarModel(ormar.Model):
    ormar_config = object()
    plain: str
    name = "not a field"
    count = ormar.Integer()
    owner = ormar.ForeignKey(User)

class SqlModel(SQLModel):
    model_config = object()
    id: int = Field(primary_key=True)
    ignored: int = other.Field()
    values: list[int] = ARRAY()
'''

    assert _findings(tmp_path, source) == [
        (4, "orm.model"),
        (8, "orm.column"),
        (9, "orm.fk"),
        (11, "orm.model"),
        (13, "orm.column"),
        (14, "orm.column"),
    ]


def test_orm_field_shape_rejects_non_names_and_preserves_foreign_targets() -> None:
    source = '''import ormar
from sqlmodel import SQLModel, Field

class OrmarModel(ormar.Model):
    self.attr: str = ormar.Integer()
    left = holder.right = ormar.Integer()
    __tablename__ = ormar.Integer()
    local = custom.Field(foreign_key="user.id")
    owner = ormar.ForeignKey(models.User)
    lookalike = custom.Field()

class SqlModel(SQLModel):
    value = custom.Integer()
    field = Field()
'''

    assert _direct_findings(source) == [
        (4, "orm.model"),
        (6, "orm.column"),
        (8, "orm.fk"),
        (9, "orm.fk"),
        (12, "orm.model"),
        (14, "orm.column"),
    ]


def test_decorators_and_parameter_markers_are_origin_sensitive(tmp_path: Path) -> None:
    source = '''from fastapi import FastAPI, Body, Query, UploadFile
from cachetools import cached
import strawberry
app = FastAPI()

@app.websocket("/ws")
async def socket(): pass

@app.exception_handler(ValueError)
async def errors(): pass

@strawberry.field
def resolve(): pass

@cached({})
def memo(): pass

@local.field
def local_field(): pass

@app.post("/x")
async def route(
    embedded: str = Body(embed=True),
    plain: str = Body(embed=False),
    unrelated_body_option: str = Body(other=True),
    constrained_body: str = Body(min_length=2),
    constrained: str = Query(min_length=2),
    query: str = Query(),
    upload: UploadFile = None,
    untyped = None,
): pass
'''

    assert _findings(tmp_path, source) == [
        (4, "route.app"),
        (6, "route.websocket"),
        (9, "exc.handler"),
        (12, "graphql.resolver"),
        (15, "cache.decorator"),
        (21, "route.method"),
        (23, "param.body_embed"),
        (24, "param.body"),
        (25, "param.body"),
        (26, "param.body"),
        (27, "param.query_strconstraint"),
        (28, "param.query"),
        (29, "param.file"),
    ]


def test_decorator_lookalikes_do_not_activate_routes_or_special_rules() -> None:
    source = '''from fastapi import FastAPI, Body
from cachetools import other
app = FastAPI()

@app.unknown()
def unknown(value: str = Body()): pass

@other
def cache_lookalike(): pass

@local.cached
def local_cached(): pass

@local.field
def local_field(): pass

@strawberry.other
def strawberry_lookalike(): pass
'''

    assert _direct_findings(source) == [(3, "route.app")]


def test_call_rules_require_the_exact_imported_api(tmp_path: Path) -> None:
    source = '''import asyncio
import multiprocessing
import cachetools
import aioboto3
import jwt
import aiometer
import gql
import s3path
import local
from fastapi import FastAPI
from authlib.integrations.requests_client import OAuth2Session

app = FastAPI()
multiprocessing.Process()
local.Process()
cachetools.TTLCache(2)
local.TTLCache(2)
aioboto3.client("s3")
jwt.decode("x")
local.decode("x")
OAuth2Session()
local.OAuth2Session()
aiometer.run_all([])
gql.Client()
local.Client()
s3path.S3Path("x")
local.S3Path("x")

async def socket(ws):
    await ws.send_json({})
    await ws.receive_json()
    await local.create_task(job())
'''

    assert _findings(tmp_path, source) == [
        (13, "route.app"),
        (14, "bg.multiprocessing"),
        (16, "cache.store"),
        (18, "ext.boto3_s3"),
        (19, "auth.jwt"),
        (21, "auth.oauth"),
        (22, "auth.oauth"),
        (23, "ext.aiometer"),
        (24, "ext.gql"),
        (26, "ext.s3path"),
        (30, "ws.json_method"),
        (31, "ws.json_method"),
    ]


def test_call_lookalikes_do_not_inherit_framework_or_library_rules() -> None:
    source = '''import jwt
import gql
import local
from dlock import lock
from fastapi import HTTPBearer
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

lock()
jwt.encode("x")
gql.transport()
HTTPBearer()
local.HTTPBearer()
JSONResponse({})
local.JSONResponse({})
one = two = TestClient(app)
holder.client = TestClient(app)
local.TestClient(app)
operation(postgresql_using="name::text")

def local_clients():
    one = two = TestClient(app)
    holder.client = TestClient(app)
'''

    assert _direct_findings(source) == [
        (9, "lock.dlock"),
        (12, "auth.security_scheme"),
        (14, "resp.class"),
        (16, "test.client"),
        (17, "test.client"),
        (19, "mig.manual"),
        (22, "test.client"),
        (23, "test.client"),
    ]


def test_isolated_call_lookalikes_do_not_consume_once_keys() -> None:
    assert _direct_findings(
        '''import local
import multiprocessing
local.Client()
local.Process()
multiprocessing.Other()
'''
    ) == []


def test_route_and_client_contexts_are_classified_separately(tmp_path: Path) -> None:
    source = '''from fastapi import FastAPI
from fastapi.testclient import TestClient
app = FastAPI()
router = object()
app.include_router(router)
for router in routers:
    app.include_router(router)

module_client = TestClient(app)

def test_local():
    client = TestClient(app)
    wrapped = factory(TestClient(app))
'''

    assert _findings(tmp_path, source) == [
        (3, "route.app"),
        (5, "route.include_static"),
        (7, "route.include_dynamic"),
        (9, "test.client"),
        (12, "test.client_local"),
        (13, "test.client"),
    ]


def test_joined_asyncio_tasks_cover_each_supported_local_shape(tmp_path: Path) -> None:
    source = '''import asyncio

async def work():
    await asyncio.create_task(job())
    annotated: object = asyncio.create_task(job())
    await annotated
    assigned = asyncio.create_task(job())
    tasks = []
    tasks.append(assigned)
    await asyncio.gather(*tasks)
    direct = []
    direct.append(asyncio.create_task(job()))
    await asyncio.wait(*direct)
    unrelated = asyncio.create_task(job())
    await something_else

def sync_work():
    task = asyncio.create_task(job())
    return task
'''

    assert _findings(tmp_path, source) == [
        (4, "bg.asyncio_joined"),
        (5, "bg.asyncio_joined"),
        (7, "bg.asyncio_joined"),
        (12, "bg.asyncio_joined"),
        (14, "bg.asyncio_loop"),
    ]


def test_join_detection_does_not_accept_lookalike_calls(tmp_path: Path) -> None:
    source = '''import asyncio

async def work():
    task = asyncio.create_task(job())
    other.append(task)
    await asyncio.gather(*different)
    tasks = []
    tasks.extend(task)
    await asyncio.gather(*tasks)
    later = asyncio.create_task(job())
    before.append(later)
    await local.gather(*before)
'''

    assert _findings(tmp_path, source) == [(4, "bg.asyncio_loop")]


def test_join_detection_rejects_each_unsupported_parent_shape() -> None:
    cases = [
        '''import asyncio
async def work():
    holder.task: object = asyncio.create_task(job())
    await holder.task
''',
        '''import asyncio
async def work():
    left = right = asyncio.create_task(job())
    await left
''',
        '''import asyncio
async def work():
    holder.tasks.append(asyncio.create_task(job()))
    await asyncio.gather(*holder.tasks)
''',
        '''import asyncio
def work():
    task: object = asyncio.create_task(job())
    awaitable(task)
''',
        '''import asyncio
async def work():
    append(asyncio.create_task(job()))
''',
        '''import asyncio
async def work():
    holder.task = asyncio.create_task(job())
    await holder.task
''',
        '''import asyncio
def work():
    task = asyncio.create_task(job())
    await task
''',
        '''import asyncio
task = asyncio.create_task(job())
''',
    ]

    for source, line in zip(cases, (3, 3, 3, 3, 3, 3, 3, 2), strict=True):
        assert _direct_findings(source) == [(line, "bg.asyncio_loop")]


def test_join_detection_requires_later_matching_accumulation_and_await() -> None:
    cases = [
        '''import asyncio
async def work():
    tasks = []
    tasks.append(task)
    task = asyncio.create_task(job())
    await asyncio.gather(*tasks)
''',
        '''import asyncio
async def work():
    task = asyncio.create_task(job())
    tasks = []
    tasks.append(other)
    await asyncio.gather(*tasks)
''',
        '''import asyncio
async def work():
    task = asyncio.create_task(job())
    tasks = []
    holder.append(task)
    await asyncio.gather(*tasks)
''',
        '''import asyncio
async def work():
    task = asyncio.create_task(job())
    self.tasks.append(task)
    await asyncio.gather(*self.tasks)
''',
        '''import asyncio
async def work():
    task = asyncio.create_task(job())
    tasks = []
    tasks.append(task)
    await local.gather(*tasks)
''',
    ]

    for source, line in zip(cases, (5, 3, 3, 3, 3), strict=True):
        assert _direct_findings(source) == [(line, "bg.asyncio_loop")]


def test_django_manager_attributes_distinguish_verbs_values_and_patches(tmp_path: Path) -> None:
    source = '''from django.db import models
from fastapi import status

class User(models.Model):
    name = models.CharField(max_length=20)

rows = User.objects.filter(name="x")
manager = User.objects
kind = User.objects.__class__
ordinary = thing.objects
not_a_patch = thing.not_objects.__class__
ok = status.HTTP_200_OK
wrong_prefix = status.OK
wrong_origin = other.HTTP_200_OK
'''

    assert _findings(tmp_path, source) == [
        (4, "orm.django.model"),
        (5, "orm.django.column"),
        (7, "orm.query.filter_exact"),
        (8, "orm.manager_value"),
        (9, "orm.manager_patch"),
        (10, "foreign.django.query"),
        (12, "resp.status_const"),
    ]


def test_django_query_verb_accepts_only_an_objects_manager() -> None:
    source = '''from django.db import models
value = thing.not_objects.filter
called = User.objects.filter(name="x")
uncalled = User.objects.filter
'''

    assert _direct_findings(source) == [
        (3, "foreign.django.query"),
        (4, "foreign.django.query"),
    ]


def test_uncalled_query_normalizes_its_non_call_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ast.Call | None] = []

    def mappings(call: ast.Call | None, _parents: dict[int, ast.AST]) -> dict[str, str]:
        seen.append(call)
        return {}

    def rule(_verb: str, call: ast.Call | None, *_args, **_kwargs) -> str:
        seen.append(call)
        return "orm.query.filter"

    monkeypatch.setattr(analyzer_scan, "plain_filter_mappings", mappings)
    monkeypatch.setattr(analyzer_scan, "query_rule", rule)

    assert _direct_findings("value = User.objects.filter\n") == [(1, "orm.query.filter")]
    assert seen == [None, None]


def test_migration_arguments_preserve_manual_and_derived_boundaries(tmp_path: Path) -> None:
    source = '''import sqlalchemy as sa
from alembic import op

op.create_table("x", sa.Column("id", sa.Integer()), sa.UniqueConstraint("id"))
op.create_table("x", thing)
op.create_table("x", sa.CheckConstraint("id > 0", ondelete="CASCADE"))
op.add_column("x", sa.Column("payload", sa.ARRAY(sa.Integer())))
op.add_column("x", sa.Column("owner", sa.ForeignKey("u.id", ondelete="CASCADE")))
op.add_column("x", sa.Column("name", sa.String()))
op.add_column("x", sa.Column("bad", object()))
op.add_column("x", sa.Column("literal", 42))
op.create_table("x", sa.Custom())
op.create_table("x", sa.UniqueConstraint(object()))
op.create_table("x", sa.Column("bad", object()), sa.Column("good", sa.String()))
op.create_table("x", sa.ForeignKeyConstraint(["owner"], ["user.id"], ondelete="CASCADE"))
op.create_table(
    "x",
    sa.Column("owner", sa.ForeignKey("user.id", ondelete="CASCADE")),
    sa.Column("bad", object()),
)
op.create_table(42, sa.Column("id", sa.Integer()))
'''

    assert _findings(tmp_path, source) == [
        (4, "mig.derived"),
        (5, "mig.schema_op"),
        (6, "mig.schema_op"),
        (7, "mig.derived"),
        (8, "mig.schema_op"),
        (9, "mig.derived"),
        (10, "mig.unmodelled_type"),
        (11, "mig.unmodelled_type"),
        (12, "mig.schema_op"),
        (13, "mig.derived"),
        (14, "mig.unmodelled_type"),
        (15, "mig.schema_op"),
        (16, "mig.schema_op"),
        (21, "mig.schema_op"),
    ]


def test_middleware_detection_uses_the_first_argument_and_named_hosts(tmp_path: Path) -> None:
    source = '''from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
app = FastAPI()
app.add_middleware(CORSMiddleware)
app.add_middleware(TrustedHostMiddleware, other=[name], allowed_hosts=["*"])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[name])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=("*", "example.test"))
app.add_middleware()
'''

    assert _findings(tmp_path, source) == [
        (4, "route.app"),
        (5, "mw.cors"),
        (6, "mw.trustedhost_noop"),
        (7, "mw.trustedhost"),
        (8, "mw.trustedhost"),
        (9, "mw.custom"),
    ]
