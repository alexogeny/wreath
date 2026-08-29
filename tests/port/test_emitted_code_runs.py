from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")


def _emit(source: str, **kwargs) -> str:
    emitted = port.emit_module(source, **kwargs)
    compile(emitted, "<ported>", "exec", dont_inherit=True)
    return emitted


def _defined_names(source: str) -> set[str]:
    """The module-level names a ported source binds, by executing its imports.

    Cheaper and more honest than a string search: it is the same question Python
    asks, minus the third-party packages that are not installed here.
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update((a.asname or a.name) for a in node.names)
    return names


def test_a_field_marker_that_stays_keeps_its_import() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Llama(BaseModel):\n"
        "    age: int = Field(default=1, ge=0)\n"
    )
    assert "Field" in _defined_names(emitted)
    assert "age: Annotated[int, Field(ge=0)] = 1" in emitted


def test_description_metadata_moves_to_the_first_party_field() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Llama(BaseModel):\n"
        '    name: str = Field(default="anon", description="the llama")\n'
    )
    assert "Field" in _defined_names(emitted)
    assert 'name: Annotated[str, Field(description="the llama")] = "anon"' in emitted


def test_an_ordinary_callable_default_is_not_a_field_marker() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "def initial_age():\n"
        "    return 1\n\n\n"
        "class Llama(BaseModel):\n"
        "    age: int = initial_age()\n"
    )

    assert "age: int = initial_age()" in emitted


def test_an_unannotated_model_mixin_uses_tableless_wreath_columns(tmp_path) -> None:
    source = tmp_path / "trail.py"
    source.write_text(
        "import ormar\n\n\n"
        "class TrailTimes:\n"
        "    opened_at = ormar.DateTime(timezone=True, nullable=True)\n\n\n"
        "    surveyed_on = ormar.Date()\n"
        "    quiet_at = ormar.Time(nullable=True)\n\n\n"
        "class Llama(ormar.Model, TrailTimes):\n"
        "    ormar_config = db.copy(tablename='llama')\n"
        "    id: int = ormar.Integer(primary_key=True)\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source)

    assert "class TrailTimes(Model):" in emitted
    assert "opened_at = column(TimestampTz, nullable=True)" in emitted
    assert "surveyed_on = column(Date)" in emitted
    assert "quiet_at = ormar.Time(nullable=True)" in emitted
    assert "wreath has no column type matching ormar.Time" in emitted
    assert 'class Llama(TrailTimes, table="llama"):' in emitted


def test_a_non_cors_fastapi_middleware_import_does_not_invent_cors() -> None:
    emitted = _emit(
        "from fastapi.middleware.gzip import GZipMiddleware\n\n\nmiddleware = GZipMiddleware\n"
    )

    assert "from fastapi.middleware.gzip import GZipMiddleware" in emitted
    assert "CorsPolicy" not in emitted


@pytest.mark.parametrize(
    "marker,expected,has_field",
    [
        (
            'Field(default_factory=list, description="tags")',
            'name: Annotated[str, Field(description="tags")] = field(default_factory=list)',
            True,
        ),
        (
            'Field(..., description="required")',
            'name: Annotated[str, Field(description="required")]\n',
            True,
        ),
        (
            'Field(description="also required")',
            'name: Annotated[str, Field(description="also required")]\n',
            True,
        ),
        ("Field(default=[])", "field(default_factory=list)", False),
    ],
)
def test_every_plain_field_shape_becomes_a_dataclass_default(marker, expected, has_field) -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        f"class Llama(BaseModel):\n    name: str = {marker}\n"
    )
    assert expected in emitted
    assert ("Field" in _defined_names(emitted)) is has_field


def test_an_except_clause_keeps_httpexception_importable() -> None:
    emitted = _emit(
        "from fastapi import HTTPException\n\n\n"
        "def guard():\n"
        "    try:\n"
        "        pass\n"
        "    except HTTPException:\n"
        "        raise\n"
    )
    assert "HTTPException" in _defined_names(emitted)
    assert "from wreath.exceptions import HTTPException" in emitted


def test_the_long_way_round_import_is_the_same_class() -> None:
    emitted = _emit(
        "from fastapi.exceptions import HTTPException\n\n\n"
        "def guard():\n"
        "    try:\n"
        "        pass\n"
        "    except HTTPException:\n"
        "        raise\n"
    )
    assert emitted.count("import HTTPException") == 1
    assert "wreath.exceptions" in emitted


def test_a_500_becomes_the_base_class_itself() -> None:
    emitted = _emit(
        "from fastapi import HTTPException\n\n\n"
        'def boom():\n    raise HTTPException(status_code=500, detail="no")\n'
    )
    assert 'raise HTTPException("no")' in emitted


def test_a_status_with_no_wreath_class_is_not_claimed() -> None:
    emitted = _emit(
        "from fastapi import HTTPException\n\n\n"
        'def boom():\n    raise HTTPException(status_code=502, detail="upstream")\n'
    )
    assert "exc.http_unmapped" in emitted
    assert "HTTPException(status_code=502" in emitted


def test_headers_are_not_dropped_from_an_exception() -> None:
    emitted = _emit(
        "from fastapi import HTTPException\n\n\n"
        "def boom():\n"
        "    raise HTTPException(\n"
        '        status_code=401, detail="no", headers={"WWW-Authenticate": "Bearer"}\n'
        "    )\n"
    )
    assert "exc.http_unmapped" in emitted


def test_fastapi_as_an_annotation_is_renamed_too() -> None:
    emitted = _emit(
        "from fastapi import FastAPI\n\n\n"
        "def build() -> FastAPI:\n"
        "    app: FastAPI = FastAPI()\n"
        "    return app\n"
    )
    assert "FastAPI" not in emitted.replace("# wreath-port", "")
    assert emitted.count("Wreath") >= 3


def test_a_uuid_foreign_key_imports_uuid() -> None:
    emitted = _emit(
        "import ormar\n\n"
        "base = None\n\n\n"
        "class Ranch(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="ranches")\n'
        "    id: str = ormar.UUID(primary_key=True)\n\n\n"
        "class Llama(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="llamas")\n'
        "    id: str = ormar.UUID(primary_key=True)\n"
        "    ranch: Ranch = ormar.ForeignKey(Ranch)\n"
    )
    assert "uuid.UUID" in emitted
    assert "uuid" in _defined_names(emitted)


def test_a_foreign_key_resolves_across_files() -> None:
    context = port.TreeContext(pk_types={"Ranch": "Int64"})
    emitted = _emit(
        "import ormar\n\n"
        "base = None\n"
        "from other import Ranch\n\n\n"
        "class Llama(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="llamas")\n'
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ranch: Ranch = ormar.ForeignKey(Ranch)\n",
        context=context,
    )
    assert "column(Int64, references=Ranch.id)" in emitted
    assert "orm.fk]" not in emitted  # resolved, so not flagged


def test_a_required_field_after_a_defaulted_one_still_builds() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Llama(BaseModel):\n"
        "    age: int = 1\n"
        "    name: str\n"
    )
    assert "@dataclass(kw_only=True)" in emitted
    namespace: dict = {}
    # `exec`, not `compile`: `@dataclass` raises while the class is being
    # created, which is a thing that happens at run time and nowhere earlier.
    exec(emitted, namespace)  # noqa: S102 -- the defect under test is at exec time
    assert namespace["Llama"](name="Bo").age == 1


def test_a_legal_order_is_left_as_a_plain_dataclass() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Llama(BaseModel):\n"
        "    name: str\n"
        "    age: int = 1\n"
    )
    assert "@dataclass\n" in emitted
    assert "kw_only" not in emitted
    namespace: dict = {}
    exec(emitted, namespace)  # noqa: S102 -- see above
    assert namespace["Llama"]("Bo").age == 1


def test_a_required_constraint_does_not_force_keyword_only_fields() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Llama(BaseModel):\n"
        "    age: int = Field(..., ge=0)\n"
        "    name: str\n"
    )
    assert "@dataclass\n" in emitted
    assert "kw_only" not in emitted
    assert "age: Annotated[int, Field(ge=0)]\n" in emitted


def test_a_required_query_parameter_keeps_being_required() -> None:
    emitted = _emit(
        "from fastapi import APIRouter, Query\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/search")\n'
        "async def search(page: int = 1, term: str = Query(...)):\n"
        "    return {}\n"
    )
    assert "*, " in emitted  # keyword-only, so the order is legal
    assert "term: Annotated[str, Query()]" in emitted


def test_a_default_written_as_a_keyword_is_still_a_default() -> None:
    emitted = _emit(
        "from fastapi import APIRouter, Query\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/search")\n'
        "async def search(only_active: bool = Query(default=False)):\n"
        "    return {}\n"
    )
    assert "only_active: Annotated[bool, Query()] = False" in emitted


def test_a_handler_with_no_parameters_gets_one() -> None:
    emitted = _emit(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/")\n'
        "async def home():\n"
        "    return {}\n"
    )
    assert "async def home(request: Request):" in emitted


def test_a_settings_init_that_only_calls_super_leaves_no_empty_body() -> None:
    emitted = _emit(
        "from pydantic_settings import BaseSettings\n"
        "\n"
        "\n"
        "class Settings(BaseSettings):\n"
        "    host: str = 'localhost'\n"
        "\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__()\n"
    )
    assert "__post_init__" not in emitted, "an empty __post_init__ is the bug, not the fix"
    assert "def __init__" not in emitted, "the method only called super; it should go"
    assert "host: str = 'localhost'" in emitted, "the field must survive"


def test_a_settings_init_that_only_forwards_kwargs_is_dropped() -> None:
    emitted = _emit(
        "from pydantic_settings import BaseSettings\n"
        "\n"
        "\n"
        "class Settings(BaseSettings):\n"
        "    host: str = 'localhost'\n"
        "\n"
        "    def __init__(self, **kwargs) -> None:\n"
        "        super().__init__(**kwargs)\n"
    )
    namespace: dict = {}
    exec(compile(emitted, "<ported>", "exec"), namespace)  # noqa: S102 -- the point
    assert namespace["Settings"](host="example.test").host == "example.test"


def test_a_model_whose_only_body_is_model_config_keeps_a_body() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, ConfigDict\n"
        "\n"
        "\n"
        "class Row(BaseModel):\n"
        "    model_config = ConfigDict(from_attributes=True)\n"
    )
    namespace: dict = {}
    exec(compile(emitted, "<ported>", "exec"), namespace)  # noqa: S102 -- the point
    assert "Row" in namespace
