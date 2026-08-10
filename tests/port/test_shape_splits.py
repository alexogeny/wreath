"""Verdicts that depend on the *shape* of a construct, not on its name.

``test_query_classification`` established the pattern for ``.objects.filter()``:
the verb names the candidate, and the arguments decide the verdict. The same
question turns out to be the right one for four more of the biggest remaining
clusters, and in each the answer splits a blanket "needs review" in two.

- A ``BaseSettings`` class of plain scalars with literal defaults is mechanical
  field by field; one validator, container type or sub-group and it is not.
- ``status_code=`` is determined when wreath's own coercion table already says
  which response class the return becomes, and not when the return's runtime
  type decides it.
- An ``@asynccontextmanager`` lifespan splits at the ``yield`` exactly when the
  two halves are independent.
- A ``@strawberry.type`` is a deletion exactly when it *is* the model's columns.

The half that stays under review is the point of each pair. A wrong silent
conversion costs more than an honest TODO earns, and the ``status_code`` case is
the proof: the emitter used to wrap any single-``return`` body in
``JSONResponse(...)``, which is broken code whenever the handler returns a DTO.
"""
import pytest

port = pytest.importorskip("wreath.port")


def _analyze(tmp_path, source: str, name: str = "module.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return port.analyze(path).findings


def _rule_ids(tmp_path, source: str, name: str = "module.py") -> list[str]:
    return [f.rule_id for f in _analyze(tmp_path, source, name)]


def _one(tmp_path, source: str, prefix: str):
    matches = [f for f in _analyze(tmp_path, source) if f.rule_id.startswith(prefix)]
    assert len(matches) == 1, [f.rule_id for f in matches]
    return matches[0]


# --- settings: mechanical field by field, or not at all ---------------------------

_SETTINGS_HEAD = "from pydantic_settings import BaseSettings, SettingsConfigDict\n"


def test_kw_only_model_is_translated_when_tree_has_no_positional_construction(
    tmp_path,
) -> None:
    source = (
        "from pydantic import BaseModel\n"
        "class Llama(BaseModel):\n"
        "    nickname: str | None = None\n"
        "    name: str\n"
        "value = Llama(name='Ada')\n"
    )

    finding = _one(tmp_path, source, "pydantic.model_kw_only")

    assert finding.rule_id == "pydantic.model_kw_only_exact"
    assert finding.tag == port.TRANSLATED


def test_kw_only_model_stays_reviewable_when_another_module_calls_it_positionally(
    tmp_path,
) -> None:
    (tmp_path / "models.py").write_text(
        "from pydantic import BaseModel\n"
        "class Llama(BaseModel):\n"
        "    nickname: str | None = None\n"
        "    name: str\n",
        encoding="utf-8",
    )
    (tmp_path / "use.py").write_text("value = Llama(None, 'Ada')\n", encoding="utf-8")

    finding = next(
        item for item in port.analyze(tmp_path).findings
        if item.rule_id.startswith("pydantic.model_kw_only")
    )

    assert finding.rule_id == "pydantic.model_kw_only"
    assert finding.tag == port.NEEDS_REVIEW


def test_a_scalar_settings_class_is_translated(tmp_path) -> None:
    """`str`/`int`/`float`/`bool` with a literal default is the whole decision.

    pydantic-settings reads the field name from the environment; `load_env`
    returns `dict[str, str]`; the four scalar conversions are the only ones that
    need no policy. That makes the class a rewrite with nothing left to choose.
    """
    source = _SETTINGS_HEAD + (
        "class Settings(BaseSettings):\n"
        "    database_url: str\n"
        "    request_timeout_s: float = 10.0\n"
        "    debug: bool = False\n"
    )
    finding = _one(tmp_path, source, "settings.class")
    assert finding.rule_id == "settings.class_env"
    assert finding.tag == port.TRANSLATED


def test_a_plain_graphql_output_points_at_the_native_dataclass_surface(
    tmp_path,
) -> None:
    source = (
        "import strawberry\n"
        "@strawberry.type\n"
        "class TrekSummary:\n"
        "    label: str\n"
        "    count: int\n"
    )

    finding = _one(tmp_path, source, "graphql.type")

    assert finding.rule_id == "graphql.type_dataclass"
    assert "GraphQL(dataclasses=" in finding.message


def test_the_translated_settings_message_names_the_required_variables(tmp_path) -> None:
    """A field with no default is a variable the app cannot boot without.

    That list is exactly `run(app, required_env=[...])`, and it is derivable, so
    the report hands it over rather than describing how to compute it.
    """
    source = _SETTINGS_HEAD + (
        "class Settings(BaseSettings):\n"
        "    database_url: str\n"
        "    log_level: str = 'INFO'\n"
    )
    finding = _one(tmp_path, source, "settings.class")
    assert "required_env=[DATABASE_URL]" in finding.message
    assert "LOG_LEVEL" not in finding.message


def test_a_literal_env_prefix_is_carried_into_the_message(tmp_path) -> None:
    source = _SETTINGS_HEAD + (
        "class Settings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(env_prefix='TUMBLEWEED_', extra='ignore')\n"
        "\n"
        "    database_url: str\n"
    )
    finding = _one(tmp_path, source, "settings.class")
    assert finding.rule_id == "settings.class_env"
    assert "required_env=[TUMBLEWEED_DATABASE_URL]" in finding.message


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("    hosts: list[str] = []\n", "a container is JSON-decoded from the variable"),
        ("    timeout: int | None = None", "an optional is not one of the four conversions"),
        ("    port: int = Field(default=5432, ge=1)", "a Field marker carries validation"),
        ("    started: str = default_now()", "a computed default is not a literal"),
    ],
)
def test_a_non_scalar_field_holds_its_class_back(tmp_path, body, why) -> None:
    source = _SETTINGS_HEAD + "class Settings(BaseSettings):\n" + body + "\n"
    ids = _rule_ids(tmp_path, source)
    assert "settings.class" in ids, why
    assert "settings.class_env" not in ids, why
    assert "settings.field_complex" in ids, why


def test_a_validator_in_a_settings_class_holds_it_back(tmp_path) -> None:
    """Every field is scalar, but the class is not: the validator has to go somewhere."""
    source = _SETTINGS_HEAD + (
        "from pydantic import field_validator\n"
        "class Settings(BaseSettings):\n"
        "    database_url: str\n"
        "\n"
        "    @field_validator('database_url')\n"
        "    def check(cls, v):\n"
        "        return v\n"
    )
    assert "settings.class" in _rule_ids(tmp_path, source)


def test_an_unreadable_config_key_holds_the_class_back(tmp_path) -> None:
    """`env_nested_delimiter` changes where values come from, so it is not ignored."""
    source = _SETTINGS_HEAD + (
        "class Settings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(env_nested_delimiter='__')\n"
        "\n"
        "    database_url: str\n"
    )
    assert "settings.class_env" not in _rule_ids(tmp_path, source)


def test_a_composed_sub_group_still_needs_a_decision(tmp_path) -> None:
    """The values carry across; the access path and the JSON-blob form do not."""
    source = _SETTINGS_HEAD + (
        "class Twilio(BaseSettings):\n"
        "    TWILIO_SID: str = ''\n"
        "\n"
        "class Settings(BaseSettings):\n"
        "    twilio: Twilio = Twilio()\n"
    )
    findings = {f.rule_id: f for f in _analyze(tmp_path, source)}
    assert findings["settings.nested"].tag == port.NEEDS_REVIEW
    assert "flatten" in findings["settings.nested"].message
    assert findings["settings.class"].tag == port.NEEDS_REVIEW


# --- status_code: wreath's coercion table decides, or a human does ----------------

_ROUTE_HEAD = "from fastapi import APIRouter\nrouter = APIRouter()\n"


def _handler(status: str, body: str) -> str:
    return _ROUTE_HEAD + f"@router.post('/x', status_code={status})\nasync def h():\n{body}"


@pytest.mark.parametrize(
    ("body", "expected", "why"),
    [
        ("    return {'ok': True}\n", "route.status_code_return",
         "wreath sends a dict through JSONResponse anyway"),
        ("    x = 1\n    if x:\n        raise ValueError\n    return [1, 2]\n",
         "route.status_code_return", "a raise is not a second return"),
        ("    return 7\n", "route.status_code_return", "a number is JSON too"),
        ("    return 'created'\n", "route.status_code_text",
         "a str return is text/plain in wreath, so JSONResponse would change the type"),
    ],
)
def test_a_literal_return_makes_the_status_determined(tmp_path, body, expected, why) -> None:
    assert _one(tmp_path, _handler("201", body), "route.status_code").rule_id == expected, why


def test_a_returned_response_already_carries_the_status(tmp_path) -> None:
    """The route-level `status_code=` was dead: the response's own status wins."""
    source = (
        "from fastapi import APIRouter, status\n"
        "from fastapi.responses import JSONResponse\n"
        "router = APIRouter()\n"
        "@router.post('/x', status_code=status.HTTP_201_CREATED)\n"
        "async def h():\n"
        "    body = {'id': 1}\n"
        "    return JSONResponse(body, status_code=201)\n"
    )
    finding = _one(tmp_path, source, "route.status_code")
    assert finding.rule_id == "route.status_code_response"
    assert finding.tag == port.TRANSLATED


def test_a_bodiless_status_with_no_return_is_determined(tmp_path) -> None:
    """204 needs `Response(status=204)`: wreath turns a bare `None` into a 200 `null`."""
    source = _handler("204", "    await drop()\n")
    finding = _one(tmp_path, source, "route.status_code")
    assert finding.rule_id == "route.status_code_empty"
    assert finding.tag == port.TRANSLATED


def test_a_bodiless_status_that_returns_a_value_is_a_contradiction(tmp_path) -> None:
    source = _handler("204", "    return {'ok': True}\n")
    finding = _one(tmp_path, source, "route.status_code")
    assert finding.rule_id == "route.status_code_empty_body"
    assert finding.tag == port.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("    return payload\n", "the runtime type picks the response class"),
        ("    return await repo.get()\n", "same, behind a call"),
        ("    if flag:\n        return {'a': 1}\n    return {'b': 2}\n",
         "two returns are two statuses to decide"),
    ],
)
def test_a_non_literal_return_still_needs_review(tmp_path, body, why) -> None:
    assert _one(tmp_path, _handler("201", body), "route.status_code").rule_id \
        == "route.status_code", why


def test_a_variable_status_code_still_needs_review(tmp_path) -> None:
    source = _handler("CREATED", "    return {'ok': True}\n")
    assert _one(tmp_path, source, "route.status_code").rule_id == "route.status_code"


DTO_HANDLER = (
    "from fastapi import APIRouter\n"
    "from pydantic import BaseModel\n"
    "router = APIRouter()\n"
    "class Manifest(BaseModel):\n"
    "    name: str\n"
    "@router.post('/x', status_code=201)\n"
    "async def submit(manifest: Manifest):\n"
    "    return manifest\n"
)


def test_the_emitter_does_not_wrap_a_dto_return_in_a_json_response(tmp_path) -> None:
    """The regression this split exists for.

    `JSONResponse(<dataclass>)` raises in wreath — `_json.dumps` has no dataclass
    encoder and `dataclasses.asdict` is the documented step. The emitter used to
    produce exactly that for any single-`return` handler, which is a port that
    fails on its first request rather than one that fails review.
    """
    path = tmp_path / "intake.py"
    path.write_text(DTO_HANDLER, encoding="utf-8")
    emitted = port.emit_module(path)
    assert "JSONResponse(manifest" not in emitted
    assert "return manifest" in emitted
    # And the kwarg stays, because dropping it would leave the route answering 200.
    assert "status_code=201" in emitted


def test_the_emitter_still_wraps_a_literal_return(tmp_path) -> None:
    path = tmp_path / "routes.py"
    path.write_text(_handler("202", "    await queue()\n    return {'queued': True}\n"),
                    encoding="utf-8")
    emitted = port.emit_module(path)
    assert "JSONResponse({'queued': True}, status=202)" in emitted
    assert "status_code=202" not in emitted


# --- lifespan: a partition of the body, or a shared name to place -----------------

_LIFESPAN_HEAD = (
    "from contextlib import asynccontextmanager\n"
    "from fastapi import FastAPI\n"
)


def test_an_independent_lifespan_splits_at_the_yield(tmp_path) -> None:
    source = _LIFESPAN_HEAD + (
        "@asynccontextmanager\n"
        "async def lifespan(app: FastAPI):\n"
        "    await connect()\n"
        "    yield\n"
        "    await disconnect()\n"
    )
    finding = _one(tmp_path, source, "lifespan.")
    assert finding.rule_id == "lifespan.split"
    assert finding.tag == port.TRANSLATED
    assert "on_startup" in finding.message and "on_shutdown" in finding.message


def test_a_name_crossing_the_yield_is_named_in_the_message(tmp_path) -> None:
    """The two halves are separate functions, so a shared local needs a home."""
    source = _LIFESPAN_HEAD + (
        "import asyncio\n"
        "@asynccontextmanager\n"
        "async def lifespan(app: FastAPI):\n"
        "    task = asyncio.create_task(worker())\n"
        "    yield\n"
        "    task.cancel()\n"
    )
    finding = _one(tmp_path, source, "lifespan.")
    assert finding.rule_id == "lifespan.ctx"
    assert "task" in finding.message and "app.state" in finding.message


def test_a_child_task_awaited_by_its_creator_is_not_a_background_service(
    tmp_path,
) -> None:
    source = (
        "import asyncio\n"
        "async def handler():\n"
        "    task = asyncio.create_task(fetch_one())\n"
        "    other = await fetch_two()\n"
        "    return await task, other\n"
    )
    finding = _one(tmp_path, source, "bg.asyncio")
    assert finding.rule_id == "bg.asyncio_joined"
    assert finding.tag == port.TRANSLATED


def test_an_unjoined_task_still_requires_supervision(tmp_path) -> None:
    source = "import asyncio\nasync def start():\n    asyncio.create_task(worker())\n"
    finding = _one(tmp_path, source, "bg.asyncio")
    assert finding.rule_id == "bg.asyncio_loop"
    assert finding.tag == port.NEEDS_REVIEW


def test_a_lifespan_yielding_state_needs_review(tmp_path) -> None:
    source = _LIFESPAN_HEAD + (
        "@asynccontextmanager\n"
        "async def lifespan(app: FastAPI):\n"
        "    yield {'pool': None}\n"
    )
    finding = _one(tmp_path, source, "lifespan.")
    assert finding.rule_id == "lifespan.ctx"
    assert "app.state" in finding.message


def test_a_yield_inside_a_context_manager_is_not_a_partition(tmp_path) -> None:
    source = _LIFESPAN_HEAD + (
        "@asynccontextmanager\n"
        "async def lifespan(app: FastAPI):\n"
        "    async with pool() as p:\n"
        "        yield\n"
    )
    finding = _one(tmp_path, source, "lifespan.")
    assert finding.rule_id == "lifespan.ctx"
    assert "exit" in finding.message


def test_an_ordinary_async_context_manager_is_not_a_lifespan(tmp_path) -> None:
    """A false positive is worse than a missing finding here.

    `contextlib.asynccontextmanager` is stdlib. An advisory-lock helper written
    with it needs no porting at all, and "split at the yield into
    on_startup/on_shutdown" is advice about a function that has no startup.
    """
    source = (
        "from contextlib import asynccontextmanager\n"
        "@asynccontextmanager\n"
        "async def ranch_lock(key: str):\n"
        "    async with acquire(key) as lock:\n"
        "        yield lock\n"
    )
    assert not [r for r in _rule_ids(tmp_path, source) if r.startswith("lifespan.")]


def test_a_lifespan_named_anything_is_found_through_the_app(tmp_path) -> None:
    """The signal is being handed to the app as `lifespan=`, not the function's name."""
    source = _LIFESPAN_HEAD + (
        "@asynccontextmanager\n"
        "async def boot(application):\n"
        "    await connect()\n"
        "    yield\n"
        "    await disconnect()\n"
        "\n"
        "app = FastAPI(lifespan=boot)\n"
    )
    assert "lifespan.split" in _rule_ids(tmp_path, source)


# --- strawberry types: a mirror is a deletion, a subset is a widening -------------

_MODELS = (
    "import ormar\n"
    "class Llama(ormar.Model):\n"
    "    ormar_config = base.copy(tablename='llama')\n"
    "    id: int = ormar.Integer(primary_key=True)\n"
    "    name: str = ormar.String(max_length=10)\n"
    "    grade: int = ormar.Integer()\n"
)


def _graph(tmp_path, graph_source: str):
    (tmp_path / "models.py").write_text(_MODELS, encoding="utf-8")
    (tmp_path / "graph.py").write_text(graph_source, encoding="utf-8")
    return [f for f in port.analyze(tmp_path).findings if f.rule_id.startswith("graphql.type")]


def test_a_type_that_is_exactly_the_model_is_a_deletion(tmp_path) -> None:
    """The `strawberry.auto` argument, extended to the class that encloses it.

    An `auto` field emits nothing because wreath derives fields from the model.
    When *every* field is `auto` and they are the model's full column set, the
    class as a whole emits nothing either.
    """
    (finding,) = _graph(tmp_path, (
        "import strawberry\n"
        "@strawberry.type\n"
        "class Llama:\n"
        "    id: strawberry.auto\n"
        "    name: strawberry.auto\n"
        "    grade: strawberry.auto\n"
    ))
    assert finding.rule_id == "graphql.type_mirror"
    assert finding.tag == port.TRANSLATED


def test_a_type_exposing_fewer_columns_than_the_model_is_a_widening(tmp_path) -> None:
    """Exposure in wreath is per model, not per field, so deleting the class publishes
    the columns the strawberry type deliberately left out — and the message names them."""
    (finding,) = _graph(tmp_path, (
        "import strawberry\n"
        "@strawberry.type\n"
        "class Llama:\n"
        "    id: strawberry.auto\n"
        "    name: strawberry.auto\n"
    ))
    assert finding.rule_id == "graphql.type"
    assert finding.tag == port.NEEDS_REVIEW
    assert "grade" in finding.message


def test_a_snake_case_field_is_a_rename_on_the_wire(tmp_path) -> None:
    """Strawberry camel-cases field names by default; wreath emits the column name.

    So `fleece_kg` is `fleeceKg` in the old schema and `fleece_kg` in the new one.
    Every client sees that, which makes it a decision rather than a deletion.
    """
    (tmp_path / "models.py").write_text(
        "import ormar\n"
        "class Llama(ormar.Model):\n"
        "    ormar_config = base.copy(tablename='llama')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    fleece_kg: float = ormar.Float()\n",
        encoding="utf-8",
    )
    (tmp_path / "graph.py").write_text(
        "import strawberry\n"
        "@strawberry.type\n"
        "class Llama:\n"
        "    id: strawberry.auto\n"
        "    fleece_kg: strawberry.auto\n",
        encoding="utf-8",
    )
    (finding,) = [f for f in port.analyze(tmp_path).findings
                  if f.rule_id.startswith("graphql.type")]
    assert finding.rule_id == "graphql.type"
    assert "fleece_kg" in finding.message


def test_an_input_type_is_never_a_derived_object_type(tmp_path) -> None:
    (finding,) = _graph(tmp_path, (
        "import strawberry\n"
        "@strawberry.input\n"
        "class Llama:\n"
        "    id: strawberry.auto\n"
        "    name: strawberry.auto\n"
        "    grade: strawberry.auto\n"
    ))
    assert finding.rule_id == "graphql.type"


def test_a_type_with_a_resolver_is_not_just_a_mirror(tmp_path) -> None:
    (finding,) = _graph(tmp_path, (
        "import strawberry\n"
        "@strawberry.type\n"
        "class Llama:\n"
        "    id: strawberry.auto\n"
        "    name: strawberry.auto\n"
        "    grade: strawberry.auto\n"
        "\n"
        "    @strawberry.field\n"
        "    def trek_count(self) -> int:\n"
        "        return 0\n"
    ))
    assert finding.rule_id == "graphql.type"
    assert "resolver" in finding.message


def test_a_type_with_no_matching_model_says_so(tmp_path) -> None:
    (finding,) = _graph(tmp_path, (
        "import strawberry\n"
        "@strawberry.type\n"
        "class Alpaca:\n"
        "    id: strawberry.auto\n"
    ))
    assert finding.rule_id == "graphql.type"
    assert "Alpaca" in finding.message


# --- ORM dataclass projections -----------------------------------------------


def test_a_literal_get_pydantic_projection_is_translated(tmp_path) -> None:
    source = (
        "from ormar import Model\n"
        "class Llama(Model):\n"
        "    pass\n"
        "LlamaName = Llama.get_pydantic(include={'name'})\n"
    )

    finding = next(
        item for item in _analyze(tmp_path, source)
        if item.rule_id.startswith("pydantic.get_pydantic")
    )

    assert finding.rule_id == "pydantic.get_pydantic_exact"
    assert finding.tag == port.TRANSLATED


def test_a_dynamic_or_nested_get_pydantic_projection_stays_unsupported(tmp_path) -> None:
    source = (
        "fields = names()\n"
        "Dynamic = Llama.get_pydantic(include=fields)\n"
        "OptionalLlama = make_optional(Llama.get_pydantic(exclude={'id'}))\n"
    )

    findings = [
        item for item in _analyze(tmp_path, source)
        if item.rule_id.startswith("pydantic.get_pydantic")
    ]

    assert [item.rule_id for item in findings] == [
        "pydantic.get_pydantic",
        "pydantic.get_pydantic",
    ]
    assert all(item.tag == port.UNSUPPORTED for item in findings)


def test_literal_get_pydantic_projection_is_emitted_as_a_named_dataclass(tmp_path) -> None:
    source = (
        "from ormar import Model\n"
        "class Llama(Model):\n"
        "    pass\n"
        "class LlamaCreate(Llama.get_pydantic(include={'name'})):\n"
        "    reason: str\n"
    )

    emitted = port.emit_module(source)

    assert "from dataclasses import dataclass" in emitted
    assert "model_dataclass" in emitted.splitlines()[4]
    assert "@dataclass(kw_only=True)" in emitted
    assert "model_dataclass(Llama, include={'name'}, name='_LlamaCreateFields')" in emitted
    assert "get_pydantic" not in emitted


# --- the emitter's own output has to compile -------------------------------------


def test_the_emitter_never_duplicates_the_request_parameter(tmp_path) -> None:
    """`ast.parse` accepts a duplicate argument; CPython refuses to compile it.

    So the emitter's round-trip guard cannot catch this on its own, and a handler
    that declared `request` anywhere but first used to come out as
    `async def h(request: Request, payload, request: Request)`.
    """
    source = (
        "from fastapi import APIRouter, Request\n"
        "router = APIRouter()\n"
        "@router.post('/x')\n"
        "async def h(payload: dict, request: Request):\n"
        "    return payload\n"
    )
    path = tmp_path / "routes.py"
    path.write_text(source, encoding="utf-8")
    emitted = port.emit_module(path)
    compile(emitted, "<emitted>", "exec")
