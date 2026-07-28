"""The ported file has to *run*, not merely parse.

Every case here is a shape a FastAPI/ormar application writes, and each one came
out of `wreath port` looking finished and then failed the moment Python touched
it. They are grouped by how they failed, because the two failure
modes need different guards and only one of them is visible to `ast.parse`:

* **a name with no import.** `Field`, `HTTPException`, `uuid`, `FastAPI` and
  `BaseModel` have all gone missing this way — each leaving a module that
  parsed, compiled, and raised `NameError` on import.
  The cause was one rule applied too eagerly: a name was dropped from its import
  whenever the emitter *recognized* it, whether or not every use of it had gone.
* **a class that cannot be created.** A `BaseModel` may declare a required
  field after a defaulted one. Pydantic does not care; `@dataclass` raises
  `TypeError` at class-creation time — which neither `ast.parse` nor `compile`
  sees, so the round-trip guard passed it.

`compile()` rather than `ast.parse()` is deliberate in the first group: a
duplicate parameter is a *compile* error, not a syntax error, so the emitter's
own round-trip guard once let `async def f(request, x, request)` through.
"""
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


# --- a name is only dropped when every use of it is gone ------------------------


def test_a_field_marker_that_stays_keeps_its_import() -> None:
    """`Field(ge=...)` needs a person, so `Field` has to still be importable."""
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Llama(BaseModel):\n"
        "    age: int = Field(default=1, ge=0)\n"
    )
    assert "Field" in _defined_names(emitted)
    assert "Field(default=1, ge=0)" in emitted


def test_a_field_marker_that_goes_takes_its_import_with_it() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Llama(BaseModel):\n"
        '    name: str = Field(default="anon", description="the llama")\n'
    )
    assert "Field" not in _defined_names(emitted)
    assert 'name: str = "anon"' in emitted


@pytest.mark.parametrize(
    "marker,expected",
    [
        ('Field(default_factory=list, description="tags")', "field(default_factory=list)"),
        ('Field(..., description="required")', "name: str\n"),
        ('Field(description="also required")', "name: str\n"),
        ("Field(default=[])", "field(default_factory=list)"),
    ],
)
def test_every_plain_field_shape_becomes_a_dataclass_default(marker, expected) -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        f"class Llama(BaseModel):\n    name: str = {marker}\n"
    )
    assert expected in emitted
    assert "Field" not in _defined_names(emitted)


def test_an_except_clause_keeps_httpexception_importable() -> None:
    """`except HTTPException` has no call to rewrite, so the name has to survive."""
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
    """`fastapi.exceptions.HTTPException` used to be imported twice, under one name."""
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
    """`wreath.exceptions.HTTPException` declares `status = 500`, so it is the target."""
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
    """fastapi takes a dict; wreath takes byte pairs. Silence here loses a 401's challenge."""
    emitted = _emit(
        "from fastapi import HTTPException\n\n\n"
        "def boom():\n"
        "    raise HTTPException(\n"
        '        status_code=401, detail="no", headers={"WWW-Authenticate": "Bearer"}\n'
        "    )\n"
    )
    assert "exc.http_unmapped" in emitted


def test_fastapi_as_an_annotation_is_renamed_too() -> None:
    """`app: FastAPI` outnumbers `FastAPI()` in real code."""
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
    """A model is almost never declared in the file that points at it."""
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
    assert "orm.fk]" not in emitted            # resolved, so not flagged


# --- a class that cannot be created ---------------------------------------------


def test_a_required_field_after_a_defaulted_one_still_builds() -> None:
    """Pydantic ignores declaration order; `@dataclass` raises at class creation."""
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


def test_a_constraint_left_in_place_counts_as_a_default() -> None:
    """`= Field(..., ge=0)` stays written, so it occupies the defaulted position."""
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Llama(BaseModel):\n"
        "    age: int = Field(..., ge=0)\n"
        "    name: str\n"
    )
    assert "@dataclass(kw_only=True)" in emitted


def test_a_required_query_parameter_keeps_being_required() -> None:
    """`Query(...)` is FastAPI's *required*; wreath spells it with no default at all."""
    emitted = _emit(
        "from fastapi import APIRouter, Query\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/search")\n'
        "async def search(page: int = 1, term: str = Query(...)):\n"
        "    return {}\n"
    )
    assert "*, " in emitted                    # keyword-only, so the order is legal
    assert "term: Annotated[str, Query()]" in emitted


def test_a_default_written_as_a_keyword_is_still_a_default() -> None:
    """`Query(default=False)` is not a required parameter, whatever it looks like."""
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
