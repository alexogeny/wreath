"""`wreath new` -- a project that already runs, so nobody has to assemble one.

The first half hour of a wreath project is spent on things that are each
written down somewhere and are nowhere written down together: that the dotenv
dialect has no comment syntax, that creating a schema is a separate statement
from applying an artifact, that `wreath migrations apply` does not reuse request
credentials, that a handler takes `request` first and declares its parameters in
`Annotated`. None of that is interesting and all of it is load-bearing.

So this writes it out once, correctly, and `tests/test_scaffold.py` imports the
result and drives a request through it -- which is the point. **A scaffold's
only value is that its output is right**, and the way a template stops being
right is that the framework moves underneath it while the template still
renders. Reading the generated text proves nothing; running it proves it.

Three deliberate limits:

* **It refuses a directory with anything in it.** No `--force`. Overwriting
  somebody's work to save them a `mkdir` is not a trade this makes.
* **It generates the minimum that is real**, rather than a demonstration of
  every subsystem. An items context shows wire contracts, one typed port, one
  adapter, a router, and a test that supplies its own adapter. A scaffold
  showing thirty subsystems is one nobody reads.
* **Nothing generated is a build product.** `--frontend react` writes the app
  shell and gitignores `web/src/api/`, because the client is derived from the
  route table by `wreath typegen` and a copy of it in git is a client that lies
  about the API the moment somebody edits a handler.
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ScaffoldError", "create", "plan"]


class ScaffoldError(Exception):
    """A refusal `wreath new` makes before writing anything."""


@dataclass(frozen=True, slots=True)
class Options:
    """One resolved `wreath new` invocation."""

    name: str
    directory: Path
    frontend: str = "none"
    profile: str = "service"
    database: str = "none"
    tenancy: bool = False
    forge: str = "none"

    @property
    def prefix(self) -> str:
        """The environment-variable prefix, from the package name."""
        return self.name.upper()

    @property
    def target(self) -> Path:
        return self.directory / self.name


def plan(options: Options) -> dict[str, str]:
    """Every file the project is made of, as `relative path -> content`.

    Built whole before anything is written, so a refusal cannot leave half a
    project on disk.
    """
    name = options.name
    files: dict[str, str] = {
        "pyproject.toml": _pyproject(options),
        ".gitignore": _gitignore(options),
        ".env.example": _env_example(options),
        "README.md": _readme(options),
        f"{name}/__init__.py": _package_init(options),
        f"{name}/py.typed": "",
        f"{name}/config.py": _config(options),
        f"{name}/app.py": _app(options),
        "tests/test_items.py": _tests(options),
    }
    if options.profile == "modular-monolith":
        files.update(
            {
                "AGENTS.md": _agents(options),
                f"{name}/adapters/__init__.py": _adapters(options),
                f"{name}/adapters/memory.py": _memory_adapter(options),
                f"{name}/domains/__init__.py": '"""Bounded contexts."""\n',
                f"{name}/domains/items/__init__.py": '"""The items context."""\n',
                f"{name}/domains/items/contracts.py": _contracts(options),
                f"{name}/domains/items/ports.py": _ports(options),
                f"{name}/domains/items/router.py": _items_router(options),
            }
        )
    else:
        files.update(
            {
                f"{name}/adapters.py": _adapters(options),
                f"{name}/contracts.py": _contracts(options),
                f"{name}/ports.py": _ports(options),
                f"{name}/routers/__init__.py": _routers_init(options),
                f"{name}/routers/items.py": _items_router(options),
            }
        )
    if options.database == "postgres":
        files[f"{name}/models.py"] = _models(options)
        files["tests/test_models.py"] = _model_tests(options)
    if options.tenancy:
        files[f"{name}/tenants.py"] = _tenants(options)
        files["tests/test_tenancy.py"] = _tenancy_tests(options)
    if options.frontend == "react":
        files["web/package.json"] = _web_package_json(options)
        files["web/tsconfig.json"] = _web_tsconfig(options)
        files["web/index.html"] = _web_index_html(options)
        files["web/src/main.tsx"] = _web_main(options)
        files["web/src/App.tsx"] = _web_app(options)
    if options.forge != "none":
        from ._ci import plan as ci_plan
        from ._ci import render as ci_render

        files.update(ci_render(ci_plan(name), options.forge))
    return files


def create(options: Options) -> list[str]:
    """Write the project. Returns the paths written, relative to the target.

    Raises `ScaffoldError` before touching the filesystem when the name is not a
    package name or the target directory holds anything at all.
    """
    from ._ci import FORGES

    _check_name(options.name)
    if options.forge != "none" and options.forge not in FORGES:
        raise ScaffoldError(f"unknown forge {options.forge!r}; supported: {', '.join(FORGES)}")
    if options.profile not in ("service", "modular-monolith"):
        raise ScaffoldError(
            f"unknown profile {options.profile!r}; supported: service, modular-monolith"
        )
    if options.tenancy and options.database != "postgres":
        raise ScaffoldError(
            "--tenancy needs --database postgres: tenant isolation is a schema and a "
            "PostgreSQL role per tenant, so there is nothing to isolate without one."
        )
    target = options.target
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(
            f"{target} is not empty; wreath new never writes over an existing "
            "directory. Choose another name, or move what is there first."
        )
    files = plan(options)
    for relative, content in sorted(files.items()):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return sorted(files)


def _check_name(name: str) -> None:
    """The name becomes a package directory, so the rule is Python's."""
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ScaffoldError(
            f"{name!r} is not an importable Python package name: letters, digits "
            "and underscores, not starting with a digit, and not a keyword. "
            "A hyphen is the usual culprit -- use an underscore."
        )
    if name != name.lower():
        raise ScaffoldError(
            f"{name!r} is not an importable Python package name here: use lower "
            "case, so the import and the directory agree on every filesystem."
        )


# --- Python ------------------------------------------------------------------


def _pyproject(options: Options) -> str:
    return f'''[project]
name = "{options.name}"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["wreath"]

[dependency-groups]
# Installed by a bare `uv sync`, which is what the CI files run. Not
# `[project.optional-dependencies]`: a test tool is not something anybody
# installing this service should be able to ask for.
dev = ["pytest>=8.4", "ruff>=0.12", "ty>=0.0.59"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["{options.name}*"]

[tool.setuptools.package-data]
"{options.name}" = ["py.typed"]

[tool.pytest.ini_options]
# `tests/` carries no `__init__.py`, so without this the project root is not on
# `sys.path` and `import {options.name}` fails from inside a test.
pythonpath = ["."]
testpaths = ["tests"]

[tool.ruff.lint]
# On top of ruff's defaults, which are already the errors worth having.
# `I` sorts imports, so a merge cannot reorder them into a conflict.
extend-select = ["I"]

[tool.ruff.lint.isort]
# Stated rather than inferred. ruff guesses first-party from the layout, and the
# guess changes when the package moves under a `src/` directory -- at which
# point every import block in the project is suddenly mis-sorted by a rule
# nobody edited.
known-first-party = ["{options.name}"]

[tool.ty.src]
include = ["{options.name}", "tests"]
'''


def _gitignore(options: Options) -> str:
    lines = [
        "__pycache__/",
        "*.py[cod]",
        ".venv/",
        "dist/",
        "build/",
        "*.egg-info/",
        ".pytest_cache/",
        "",
        "# The real one. `.env.example` is the committed template.",
        ".env",
    ]
    if options.frontend == "react":
        lines += [
            "",
            "node_modules/",
            "web/dist/",
            "# Generated from the route table by `wreath typegen`. A copy in git",
            "# is a client that lies about the API as soon as a handler changes.",
            "web/src/api/",
        ]
    return "\n".join(lines) + "\n"


def _env_example(options: Options) -> str:
    """The committed template.

    **No comments, and no empty values.** wreath's dotenv dialect is `KEY=value`
    and has none: a `#` line raises a `ValueError` naming the line number rather
    than being skipped, so an annotated template produces a `.env` that fails to
    load on its first line. An empty value is not "keep the default" either --
    it binds as the empty string on a `str` field and fails to coerce on an
    `int` -- so every key here carries a value that actually works in
    development. What each key *means* belongs beside the dataclass that reads
    it, in `{name}/config.py`.
    """
    lines = [f"{options.prefix}_PAGE_SIZE=20"]
    if options.database == "postgres":
        lines.insert(
            0,
            f"{options.prefix}_DATABASE_URL=postgresql://wreath:wreath@127.0.0.1:55432/wreath_test",
        )
    return "\n".join(lines) + "\n"


def _package_init(options: Options) -> str:
    return f'"""The {options.name} service."""\n'


def _routers_init(options: Options) -> str:
    return '"""Route modules, gathered into the application in `app.py`."""\n'


def _config(options: Options) -> str:
    prefix = options.prefix
    database_field = ""
    database_note = ""
    if options.database == "postgres":
        database_field = """
    #: The database. **No default**: guessing at `localhost` and connecting to
    #: the wrong database is worse than refusing to start, so this is required
    #: and the error names the variable.
    database_url: str
"""
        database_note = f"""
`{prefix}_DATABASE_URL` is read here but the connection is not opened here.
`app.py` hands it to `app.postgres(...)`, which connects during the lifespan --
so importing this module, a router, or a test needs no database at all.
"""
    return f'''"""Everything the application reads before it serves a request.

Configuration is how the application *starts*; state is what it holds while
running. They never share an API -- see the config-and-state guide. Everything
here is fixed for the life of the process and read exactly once.

Values come from the process environment merged with `.env`, under the
`{prefix}_` prefix. A missing `.env` is ordinary rather than an error: CI
exports the variables instead of shipping a file.
{database_note}"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wreath.config import Environment, read_osenv

#: The dotenv this project ships an example of. Optional -- exporting the
#: variables works identically.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

#: Every variable this application reads starts with this.
PREFIX = "{prefix}"


@dataclass(frozen=True, slots=True)
class Settings:
    """One immutable snapshot of the environment, coerced and checked at boot.

    Every field maps to `{prefix}_<FIELD>`, upper-cased. A missing or malformed
    value is reported with every other one at once, rather than one boot at a
    time.
    """
{database_field}
    #: How many items one page of the list endpoint returns.
    page_size: int = 20


def _environment() -> Environment:
    """The environment, with `.env` under it when there is one.

    A process value beats the file, so a developer's exported variable always
    wins over a checked-in default.
    """
    try:
        return Environment.load(ENV_FILE, search=False)
    except FileNotFoundError:
        return Environment(read_osenv())


#: Read at import, deliberately: a value baked into a decorator has to be known
#: when the module declaring the route is imported, and setting the variable
#: afterwards would silently do nothing.
SETTINGS = _environment().bind(Settings, prefix=PREFIX)
'''


def _app(options: Options) -> str:
    wreath_imports = ["from wreath import Wreath"]
    if options.profile == "modular-monolith":
        local_imports = [
            "from .adapters import Adapters",
            "from .config import SETTINGS, Settings",
            "from .domains.items.router import build_items_router",
        ]
    else:
        local_imports = [
            "from .adapters import Adapters",
            "from .config import SETTINGS, Settings",
            "from .routers.items import build_items_router",
        ]
    database_wiring = ""
    signature = (
        "build(*, settings: Settings = SETTINGS, adapters: Adapters | None = None) -> Wreath:"
    )
    build_doc = '''"""Assemble one application from immutable settings and explicit ports.

    Passing adapters is the composition seam for tests and deployments. Nothing
    in a route reaches into a process-global container, so two applications in
    one process may use different implementations without interfering.
    """'''
    if options.database == "postgres":
        signature = (
            "build(*, settings: Settings = SETTINGS, adapters: Adapters | None = None, "
            "database: bool = True) -> Wreath:"
        )
        build_doc = '''"""Assemble the application.

    `database=False` builds everything except the database registration. The
    routes below hold their data in memory, so the test suite uses it: a suite
    that needs PostgreSQL running is a suite nobody runs on a laptop, and one
    that skips itself instead is indistinguishable from one that passed.

    Everything the database *does* reach is exercised by `tests/test_models.py`
    and by `wreath migrations detect`, which compares the declaration to a live
    schema. Serving always uses the default.
    """'''
    if options.database == "postgres":
        local_imports.append("from .models import MODELS")
        schema_mode = ""
        tenancy_wiring = ""
        if options.tenancy:
            local_imports.append("from .tenants import tenancy")
            wreath_imports += [
                "from wreath.orm import SchemaMode",
                "from wreath.tenancy import TenancyMiddleware",
            ]
            # `isolated`, not `single`: it is what compiles tenant-template
            # models to unqualified SQL, so the search path resolves them and a
            # central-schema model stays qualified.
            schema_mode = (
                ",\n            schema_mode=SchemaMode.isolated("
                'central="central", isolation="role")'
            )
            tenancy_wiring = (
                "\n        # Global, not route middleware: the binding has to exist\n"
                "        # before a route's own tape runs, or an authorization hook\n"
                "        # that reads tenant-scoped data runs unbound. Inside the\n"
                "        # database branch, because a build with no database has no\n"
                "        # tenant schema to resolve into.\n"
                "        application.add_global_middleware(TenancyMiddleware(tenancy))\n"
            )
        database_wiring = f"""
    if database:
        # The connection is opened by the lifespan, not here. wreath then
        # validates the live schema against the models and refuses to start on
        # a mismatch, which is the one moment that mismatch is cheap to find --
        # and is why `database=False` exists at all.
        application.postgres("main", dsn=settings.database_url)
        application.orm(
            database="main", models=list(MODELS){schema_mode})
{tenancy_wiring}"""
    return f'''"""The application: settings in, routers gathered, nothing else.

`build()` exists as well as `app` because a factory is what a test wants -- one
application per test, rather than one shared by all of them. `wreath run
{options.name}.app:app` serves the module-level instance; `wreath run
{options.name}.app:build --factory` calls the function.
"""

from __future__ import annotations

{chr(10).join(sorted(wreath_imports))}

{chr(10).join(sorted(local_imports))}


def {signature}
    {build_doc}
    active_adapters = adapters or Adapters.defaults()
    application = Wreath(require_access_declarations=True)
{database_wiring}    application.include_router(
        build_items_router(settings, active_adapters.catalogue)
    )
    return application


app = build()
'''


def _items_router(options: Options) -> str:
    if options.profile == "modular-monolith":
        imports = """from ...config import Settings
from .contracts import Item, ItemPage, NewItem
from .ports import Catalogue"""
    else:
        imports = """from ..config import Settings
from ..contracts import Item, ItemPage, NewItem
from ..ports import Catalogue"""
    return f'''"""HTTP delivery for the items context.

The router is a factory because its port is supplied by `app.build`. Routes
close over that typed port; they do not locate an adapter through global state.
"""

from __future__ import annotations

from typing import Annotated

from wreath import Request, Router
from wreath.auth import public
from wreath.binding import Body, Query
from wreath.exceptions import NotFound

{imports}


def build_items_router(settings: Settings, catalogue: Catalogue) -> Router:
    """Bind HTTP to the context using the adapter selected at composition."""
    items = Router(prefix="/items", tags=("items",))

    @items.get("/", summary="Every item, newest last", operation_id="listItems")
    @public()
    async def list_items(
        request: Request,
        limit: Annotated[int, Query(minimum=1, maximum=100)] = settings.page_size,
    ) -> ItemPage:
        return ItemPage(items=catalogue.all(limit))

    @items.get("/{{item_id}}", summary="One item by id", operation_id="readItem")
    @public()
    async def read_item(request: Request, item_id: int) -> Item:
        item = catalogue.get(item_id)
        if item is None:
            raise NotFound(f"no item {{item_id}}")
        return item

    @items.post("/", status_code=201, summary="Add an item", operation_id="addItem")
    @public()
    async def add_item(request: Request, body: Annotated[NewItem, Body()]) -> Item:
        return catalogue.add(body)

    return items
'''


def _contracts(options: Options) -> str:
    return '''"""Wire contracts owned by the items context."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    name: str
    price: float


@dataclass(frozen=True, slots=True)
class ItemPage:
    items: list[Item]


@dataclass(frozen=True, slots=True)
class NewItem:
    name: str
    price: float
'''


def _ports(options: Options) -> str:
    contracts = ".contracts"
    return f'''"""Ports the items context requires from infrastructure."""

from __future__ import annotations

from typing import Protocol

from {contracts} import Item, NewItem


class Catalogue(Protocol):
    def all(self, limit: int) -> list[Item]: ...

    def add(self, new: NewItem) -> Item: ...

    def get(self, item_id: int) -> Item | None: ...
'''


def _adapters(options: Options) -> str:
    if options.profile == "modular-monolith":
        return '''"""The application's concrete adapter bundle."""

from __future__ import annotations

from dataclasses import dataclass

from ..domains.items.ports import Catalogue
from .memory import MemoryCatalogue


@dataclass(frozen=True, slots=True)
class Adapters:
    catalogue: Catalogue

    @classmethod
    def defaults(cls) -> Adapters:
        return cls(catalogue=MemoryCatalogue())


__all__ = ["Adapters", "MemoryCatalogue"]
'''
    return '''"""Concrete adapters selected at the application boundary."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from .contracts import Item, NewItem
from .ports import Catalogue


class MemoryCatalogue:
    def __init__(self) -> None:
        self._items: list[Item] = []
        self._ids = count(1)

    def all(self, limit: int) -> list[Item]:
        return self._items[:limit]

    def add(self, new: NewItem) -> Item:
        item = Item(id=next(self._ids), name=new.name, price=new.price)
        self._items.append(item)
        return item

    def get(self, item_id: int) -> Item | None:
        return next((item for item in self._items if item.id == item_id), None)


@dataclass(frozen=True, slots=True)
class Adapters:
    catalogue: Catalogue

    @classmethod
    def defaults(cls) -> Adapters:
        return cls(catalogue=MemoryCatalogue())
'''


def _memory_adapter(options: Options) -> str:
    return '''"""Development and test adapters for the items context."""

from __future__ import annotations

from itertools import count

from ..domains.items.contracts import Item, NewItem


class MemoryCatalogue:
    def __init__(self) -> None:
        self._items: list[Item] = []
        self._ids = count(1)

    def all(self, limit: int) -> list[Item]:
        return self._items[:limit]

    def add(self, new: NewItem) -> Item:
        item = Item(id=next(self._ids), name=new.name, price=new.price)
        self._items.append(item)
        return item

    def get(self, item_id: int) -> Item | None:
        return next((item for item in self._items if item.id == item_id), None)
'''


def _agents(options: Options) -> str:
    return f"""# {options.name} architecture

- Put business capabilities under `{options.name}/domains/<context>/`.
- A context may import shared wire primitives, but never another context's adapter.
- Define infrastructure needs as `Protocol` ports beside the context that owns them.
- Select concrete adapters only in `{options.name}/app.py`; tests pass doubles to
  `build(settings=..., adapters=...)`.
- Every route must declare `@public()` or an authentication/authorization guard.
- Give every route a stable `operation_id`; review `wreath doctor routes
  {options.name}.app:app` when the surface changes.
"""


def _models(options: Options) -> str:
    return f'''"""The tables this service owns.

Declared here and turned into DDL by `wreath migrations`, which is the only
thing that writes to your schema -- there is no `create_all`. `app.py` passes
`MODELS` to `application.orm(...)`, and the application then refuses to start
against a database that does not match.

The loop, once:

```bash
psql "$DSN" -c 'CREATE SCHEMA IF NOT EXISTS {options.name}'
export WREATH_MIGRATION_DSN="$DSN"   # apply never reuses request credentials
wreath migrations generate {options.name}.app:app migrations/migration.bin
wreath migrations apply {options.name}.app:app migrations/migration.bin
```
"""

from __future__ import annotations

from wreath.orm import Mapped, Model, column
from wreath.orm.types import Float8, Int64, Text

#: One PostgreSQL namespace for this service. wreath's own tables live
#: elsewhere and it creates them itself; this schema holds yours.
SCHEMA = "{options.name}"


class Item(Model, table="item", schema=SCHEMA):
    """One item for sale.

    The PostgreSQL type is named explicitly and columns are `NOT NULL` unless
    you say otherwise -- both the opposite of the usual default, and both
    deliberate: a nullable column nobody chose is a query with an edge case
    nobody wrote.

    `Float8` rather than `Numeric` for the price, only because this is a
    scaffold: `Numeric` decodes to `Decimal`, which the JSON encoder refuses,
    so real money wants integer minor units or an explicit conversion on the
    way out.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    price: Mapped[float] = column(Float8)


#: Everything `app.py` compiles the registry from.
MODELS = (Item,)
'''


def _tests(options: Options) -> str:
    database_arg = ", database=False" if options.database == "postgres" else ""
    build_note = (
        """

`database=False`, because these routes hold their data in memory and a suite
that needs PostgreSQL running is a suite nobody runs. `test_models.py` covers
the declaration that does reach the database."""
        if options.database == "postgres"
        else ""
    )
    return f'''"""The project's own suite, green as delivered.

`asyncio.run` rather than an async test, deliberately: async tests need an async
pytest plugin, and wreath does not install one for you. This needs nothing but
pytest.{build_note}
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from wreath.testing import TestClient

from {options.name}.adapters import Adapters, MemoryCatalogue
from {options.name}.app import build
from {options.name}.config import SETTINGS


def _drive(scenario: Callable[[TestClient], Awaitable[None]]) -> None:
    """Run one scenario with an explicitly selected test adapter."""

    async def run() -> None:
        adapters = Adapters(catalogue=MemoryCatalogue())
        application = build(settings=SETTINGS, adapters=adapters{database_arg})
        async with TestClient(application) as client:
            await scenario(client)

    asyncio.run(run())


def test_an_empty_catalogue_lists_nothing() -> None:
    async def scenario(client: TestClient) -> None:
        response = await client.get("/items")
        assert response.status == 200
        assert response.json() == {{"items": []}}

    _drive(scenario)


def test_an_added_item_comes_back_with_an_id() -> None:
    async def scenario(client: TestClient) -> None:
        created = await client.post("/items", json={{"name": "broom", "price": 4.5}})
        assert created.status == 201
        assert created.json()["id"] == 1

        listed = await client.get("/items")
        assert [item["name"] for item in listed.json()["items"]] == ["broom"]

    _drive(scenario)


def test_an_unknown_item_is_a_problem_document_not_a_bare_404() -> None:
    """wreath answers RFC 9457, so the body is a document rather than a string.

    Two things worth knowing from this one test: the attribute is `status` and
    not `status_code` (a `TestResponse` is not an httpx response), and `headers`
    is the raw ASGI list of lowercase byte pairs rather than a mapping.

    Asserting the document as well as the code, because a route that had stopped
    being registered at all would also answer 404.
    """

    async def scenario(client: TestClient) -> None:
        response = await client.get("/items/404")
        assert response.status == 404
        assert dict(response.headers)[b"content-type"].startswith(
            b"application/problem+json")
        assert response.json()["status"] == 404

    _drive(scenario)


def test_an_unknown_body_field_is_refused_rather_than_ignored() -> None:
    """Extra fields are always rejected -- a client typo is a 422, not a no-op."""

    async def scenario(client: TestClient) -> None:
        response = await client.post(
            "/items", json={{"name": "mop", "price": 3.0, "colour": "red"}})
        assert response.status == 422

    _drive(scenario)
'''


def _model_tests(options: Options) -> str:
    return f'''"""What the model declares, checked without a database.

A declaration test is not a substitute for running against PostgreSQL -- it
cannot tell you the table exists. `wreath migrations detect {options.name}.app:app`
is what answers that, and it needs a live schema. What this catches is the half
that is cheap to get wrong and expensive to find later: a column quietly renamed,
or a table that stopped being mapped where the migration puts it.
"""

from __future__ import annotations

from {options.name}.models import MODELS, SCHEMA, Item


def test_the_registry_lists_every_model_the_app_compiles():
    assert MODELS == (Item,)


def test_the_item_table_lands_in_this_service_s_schema():
    """wreath's own tables live elsewhere and it creates them itself."""
    assert Item.__wreath_schema__ == SCHEMA
    assert Item.__wreath_table__ == "item"


def test_the_declared_columns_are_the_ones_a_migration_will_write():
    declared = {{column.python_name for column in Item.__wreath_columns__}}
    assert declared == {{"id", "name", "price"}}


def test_every_column_is_not_null_because_nothing_asked_for_null():
    """The opposite of the usual default, and the reason to state it once.

    A nullable column nobody chose is a query with an edge case nobody wrote.
    When you do want one, `column(Text, nullable=True)` says so and this test is
    where you record that you meant it.
    """
    assert [column.python_name for column in Item.__wreath_columns__
            if column.nullable] == []
'''


def _tenants(options: Options) -> str:
    return f'''"""Who this service's tenants are, and where their data lives.

The directory is **the application's own record**, deliberately: wreath never
invents tenant identity, because a framework that guessed one would be guessing
which customer's rows to serve. In development it is held in the process; in a
deployment it is a table in the central schema, read through the same
`TenantDirectory` protocol.

Provisioning a tenant is three steps that are wrong apart -- a schema, a role,
and the grants that let the role reach its own schema and read the central one:

```bash
python -c "..."   # see wreath's tenancy guide; provision_tenant does all three
{options.name} migrations apply ...    # then apply your artifact to that schema
```

The tenant is `PROVISIONING` until the artifact has been applied, and
`require_bindable()` refuses that state -- a half-migrated tenant answering a
request with a missing-relation error deep in a handler is the thing the status
exists to prevent.
"""

from __future__ import annotations

from typing import Annotated

from wreath.orm import FromORM, Session
from wreath.tenancy import (
    FromTenant,
    InMemoryTenantDirectory,
    Tenancy,
    Tenant,
    TenantHostLabel,
)

#: The spelling every tenant-scoped route uses.
#:
#: `FromTenant()` is what makes the *declarative* path the safe one. Without it
#: a route against a tenant-isolated registry is refused at compile time, and
#: the only alternative would be building a `Session` by hand in the handler
#: body -- which is how a handler ends up querying with no tenant bound at all.
TenantSession = Annotated[Session, FromORM("main", tenant=FromTenant())]

#: Development tenants. Replace with a directory backed by the central schema
#: before anything real: this one cannot see a tenant another worker provisioned.
directory = InMemoryTenantDirectory([
    Tenant(key="acme", schema="tenant_acme", role="tenant_acme"),
    Tenant(key="globex", schema="tenant_globex", role="tenant_globex"),
])

#: `source=` has **no default**. Where the tenant name comes from is a
#: deployment decision -- guessing at a subdomain is how a service that was never
#: multi-tenant on its apex starts resolving `www` as a customer. The three
#: shipped sources are `TenantHostLabel`, `TenantHeader` and
#: `TenantSessionClaim`; the last is strongest, because the name then comes from
#: state the server wrote and a caller cannot name a tenant at all.
tenancy = Tenancy(directory=directory, source=TenantHostLabel("localhost"))
'''


def _tenancy_tests(options: Options) -> str:
    return f'''"""Tenant resolution, without a database.

The isolation itself is PostgreSQL's -- a role and a grant set per tenant -- and
`wreath.tenancy`'s own suite proves that against a live server. What is worth
testing *here* is the part this project owns: which tenants exist, and how a
request names one.
"""

from __future__ import annotations

import pytest
from wreath.tenancy import TenantSuspended, UnknownTenant

from {options.name}.tenants import directory, tenancy


class _Request:
    """Just enough request to name a tenant.

    `header()` is the accessor and `headers` is the raw ASGI list of byte pairs,
    because that is what a real `wreath.Request` is. A fake that exposes a dict
    is easier to use than the thing it stands in for, and a source written
    against it passes every test here and raises on the first real request.
    """

    def __init__(self, host):
        self.headers = [(b"host", host.encode("latin-1"))]

    def header(self, name, default=None):
        wanted = name.lower().encode("latin-1")
        for key, value in self.headers:
            if key == wanted:
                return value.decode("latin-1")
        return default


def test_a_known_host_resolves_to_its_own_schema():
    assert tenancy.resolve_request(_Request("acme.localhost")).schema == "tenant_acme"


def test_two_tenants_resolve_to_different_schemas():
    """The property the whole design exists for, stated once here so a directory
    edit that collapsed two tenants onto one schema fails loudly."""
    acme = tenancy.resolve_request(_Request("acme.localhost"))
    globex = tenancy.resolve_request(_Request("globex.localhost"))
    assert acme.schema != globex.schema


def test_an_unknown_host_is_refused_rather_than_falling_back():
    """There is no default tenant. A fallback here serves a request against
    whichever schema the pooled connection last held."""
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request("nobody.localhost"))


def test_the_apex_names_no_tenant():
    """`localhost` itself is not a customer, and neither is `www`."""
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request("localhost"))


def test_suspending_a_tenant_stops_it_resolving():
    """Enforced at the bind, so no route can forget to check it."""
    import dataclasses

    from wreath.tenancy import TenantStatus

    directory.add(dataclasses.replace(
        directory.resolve("globex"), status=TenantStatus.SUSPENDED))
    try:
        with pytest.raises(TenantSuspended):
            tenancy.resolve_request(_Request("globex.localhost"))
    finally:
        directory.add(dataclasses.replace(
            directory.resolve("globex"), status=TenantStatus.ACTIVE))
'''


# --- README ------------------------------------------------------------------


def _readme(options: Options) -> str:
    name = options.name
    sections = [
        f"# {name}",
        "",
        "A wreath service, generated by `wreath new`.",
        "",
        "## Run it",
        "",
        "```bash",
        "cp .env.example .env",
        f"wreath dev {name}.app:app       # autoreload while you edit",
        f"wreath run {name}.app:app       # the native server",
        "```",
        "",
        "`.env` is `KEY=value` and **has no comment syntax**: a `#` line is a",
        "`ValueError` naming the line number, not a line that is skipped. An",
        'empty value is not "use the default" either -- it binds as the empty',
        "string, or fails to coerce. What each key means is beside the dataclass",
        "that reads it, in `" + name + "/config.py`.",
        "",
        "## Test it",
        "",
        "```bash",
        "pytest",
        "ty check",
        "```",
        "",
        "## Before you deploy",
        "",
        "```bash",
        f"wreath doctor preflight {name}.app:app --environ",
        f"wreath doctor routes {name}.app:app --write route-manifest.json",
        "```",
        "",
        "One report of everything wreath can check about a built application,",
        "and a named list of what it cannot -- each with the command that does.",
        "Commit `route-manifest.json` when you want route, wire-type, dependency,",
        "middleware, and access changes to be explicit in review; use `--check`",
        "instead of `--write` in CI.",
        "",
        "## Finding what already ships",
        "",
        "```bash",
        "wreath capabilities celery      # -> wreath.jobs, and its guide",
        "```",
        "",
        "wreath is wide on purpose. Before adding a dependency or writing a rate",
        "limiter, ask it; the answer is usually a module you already have.",
    ]
    if options.profile == "modular-monolith":
        sections += [
            "",
            "## Architecture",
            "",
            f"Business capabilities live under `{name}/domains/<context>/`.",
            "Each context owns its wire contracts, ports, and HTTP adapter; the",
            f"composition root in `{name}/app.py` is the only place that selects",
            "concrete infrastructure. Read `AGENTS.md` before adding a context.",
        ]
    if options.database == "postgres":
        sections += [
            "",
            "## The database",
            "",
            "```bash",
            "docker run -d --name " + name + "-pg \\",
            "  -e POSTGRES_PASSWORD=wreath -e POSTGRES_USER=wreath \\",
            "  -e POSTGRES_DB=wreath_test -p 55432:5432 postgres:17-alpine",
            "",
            f"psql \"$DSN\" -c 'CREATE SCHEMA IF NOT EXISTS {name}'",
            "```",
            "",
            "Creating the schema is a separate statement on purpose: a migration",
            "artifact describes tables, not which schema they land in.",
            "",
            "```bash",
            'export WREATH_MIGRATION_DSN="$DSN"   # apply never reuses request credentials',
            f"wreath migrations generate {name}.app:app migrations/migration.bin",
            f"wreath migrations apply {name}.app:app migrations/migration.bin",
            "```",
            "",
            f"See `{name}/models.py`. There is no `create_all`: the application",
            "validates its schema at startup and refuses a database that does not",
            "match, and `wreath migrations` is what changes one.",
        ]
    if options.frontend == "react":
        sections += [
            "",
            "## The front end",
            "",
            "The TypeScript client is **generated from the route table**, so it",
            "cannot drift from the API. Regenerate it whenever a handler changes:",
            "",
            "```bash",
            f"wreath typegen {name}.app:app --output web/src/api --react-query",
            "```",
            "",
            "```bash",
            "cd web && npm install && npm run dev",
            "```",
            "",
            "`web/src/api/` is gitignored deliberately: it is a build product,",
            "and a committed copy is a client that lies about the API the moment",
            "somebody edits a route. Generate it in CI as well, with `--check`,",
            "so a route change that nobody regenerated fails the build.",
        ]
    return "\n".join(sections) + "\n"


# --- the front end -----------------------------------------------------------


def _web_package_json(options: Options) -> str:
    return f'''{{
  "name": "{options.name}-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {{
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "typecheck": "tsc --noEmit"
  }},
  "dependencies": {{
    "@tanstack/react-query": "^5.62.7",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  }},
  "devDependencies": {{
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.4",
    "typescript": "^5.7.2",
    "vite": "^6.0.3"
  }}
}}
'''


def _web_tsconfig(options: Options) -> str:
    return """{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "jsx": "react-jsx",
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["src/**/*.ts", "src/**/*.tsx"]
}
"""


def _web_index_html(options: Options) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{options.name}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""


def _web_main(options: Options) -> str:
    return """import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { App } from "./App";

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
"""


def _web_app(options: Options) -> str:
    return f"""// The hooks below come from `web/src/api/`, which is generated from the route
// table. Run this before the first `npm run dev`, and again after any change to
// a handler's parameters or return type:
//
//   wreath typegen {options.name}.app:app --output web/src/api --react-query
//
// Nothing here is hand-written against the API. That is the point: a renamed
// field is a TypeScript error at build time rather than `undefined` in a
// browser.
import {{ useState }} from "react";

import {{ useAddItem, useListItems }} from "./api/react-query";

export function App() {{
  // `useListItems` and `useAddItem` are named by the routes' `operation_id`,
  // and `item` below is a generated `Item` -- so renaming a field on the server
  // is a TypeScript error here at build time rather than `undefined` in a
  // browser. Nothing in this file is hand-written against the API.
  const items = useListItems({{}});
  const addItem = useAddItem();
  const [name, setName] = useState("");

  if (items.isPending) return <p>Loading…</p>;
  if (items.error) return <p role="alert">{{items.error.message}}</p>;

  return (
    <main>
      <h1>{options.name}</h1>
      <ul>
        {{items.data?.items.map((item) => (
          <li key={{item.id}}>
            {{item.name}} — {{item.price}}
          </li>
        ))}}
      </ul>
      <form
        onSubmit={{(event) => {{
          event.preventDefault();
          addItem.mutate({{ name, price: 0 }});
          setName("");
        }}}}
      >
        <label>
          Name
          <input value={{name}} onChange={{(event) => setName(event.target.value)}} />
        </label>
        <button type="submit" disabled={{addItem.isPending}}>
          Add
        </button>
      </form>
    </main>
  );
}}
"""
