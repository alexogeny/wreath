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
  every subsystem. An items router with an app-scoped store is four concepts:
  a router, a bound body, a dependency that outlives a request, and a test that
  drives it. A scaffold showing thirty is one nobody reads.
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
    database: str = "none"

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
        f"{name}/config.py": _config(options),
        f"{name}/app.py": _app(options),
        f"{name}/routers/__init__.py": _routers_init(options),
        f"{name}/routers/items.py": _items_router(options),
        "tests/test_items.py": _tests(options),
    }
    if options.database == "postgres":
        files[f"{name}/models.py"] = _models(options)
    if options.frontend == "react":
        files["web/package.json"] = _web_package_json(options)
        files["web/tsconfig.json"] = _web_tsconfig(options)
        files["web/index.html"] = _web_index_html(options)
        files["web/src/main.tsx"] = _web_main(options)
        files["web/src/App.tsx"] = _web_app(options)
    return files


def create(options: Options) -> list[str]:
    """Write the project. Returns the paths written, relative to the target.

    Raises `ScaffoldError` before touching the filesystem when the name is not a
    package name or the target directory holds anything at all.
    """
    _check_name(options.name)
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

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["{options.name}*"]

[tool.pytest.ini_options]
# `tests/` carries no `__init__.py`, so without this the project root is not on
# `sys.path` and `import {options.name}` fails from inside a test.
pythonpath = ["."]
testpaths = ["tests"]
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
            f"{options.prefix}_DATABASE_URL="
            "postgresql://wreath:wreath@127.0.0.1:55432/wreath_test",
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
        database_field = '''
    #: The database. **No default**: guessing at `localhost` and connecting to
    #: the wrong database is worse than refusing to start, so this is required
    #: and the error names the variable.
    database_url: str
'''
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
    database_imports = ""
    database_wiring = ""
    if options.database == "postgres":
        database_imports = "\nfrom .models import MODELS"
        database_wiring = '''
    # The connection is opened by the lifespan, not here, so importing this
    # module needs no database. `validate_schema` is the framework default:
    # the application refuses to start against a schema its models do not
    # match, which is the one moment that mismatch is cheap to find.
    application.postgres("main", dsn=SETTINGS.database_url)
    application.orm(database="main", models=list(MODELS))
'''
    return f'''"""The application: settings in, routers gathered, nothing else.

`build()` exists as well as `app` because a factory is what a test wants -- one
application per test, rather than one shared by all of them. `wreath run
{options.name}.app:app` serves the module-level instance; `wreath run
{options.name}.app:build --factory` calls the function.
"""

from __future__ import annotations

from wreath import Wreath

from .config import SETTINGS{database_imports}
from .routers.items import items


def build() -> Wreath:
    """Assemble the application."""
    application = Wreath()
{database_wiring}    application.include_router(items)
    return application


app = build()
'''


def _items_router(options: Options) -> str:
    return '''"""One resource, showing the four things every wreath router does.

* a `Router` with a prefix, included by `app.py` rather than reaching for the
  application object here;
* parameters declared in `Annotated`, so the ordinary Python default stays an
  ordinary Python default and validation is compiled at startup;
* a store that outlives one request, owned explicitly through
  `Depends(..., scope="app")` -- not a module-level global, which two
  applications in one process would share without either of them saying so;
* a return annotation, which *is* the response contract wreath validates
  against.

The store is in memory, so it is per worker and it is gone when the process
restarts. That is fine for a scaffold and wrong for a deployment: run
`wreath new` with `--database postgres` for the shape that is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Annotated

from wreath import Request, Router
from wreath.binding import Body, Depends, Query
from wreath.exceptions import NotFound

from ..config import SETTINGS

items = Router(prefix="/items", tags=("items",))


@dataclass(frozen=True, slots=True)
class Item:
    """One item, as it goes out on the wire."""

    id: int
    name: str
    price: float


@dataclass(frozen=True, slots=True)
class ItemPage:
    """One page of items.

    A declared type rather than `-> dict`, because the return annotation *is*
    the response contract: wreath validates against it, the OpenAPI document
    describes it, and `wreath typegen` turns it into a TypeScript interface. A
    handler annotated `-> dict` generates a client typed `Record<string,
    unknown>`, which compiles and tells the front end nothing.
    """

    items: list[Item]


@dataclass(frozen=True, slots=True)
class NewItem:
    """One item, as it comes in.

    A separate type from `Item` on purpose: the client does not choose the id,
    and a request body that accepts one is a request body that can collide.
    Extra fields are always rejected, so a typo in a client is a 422 rather than
    a value that silently does nothing.
    """

    name: str
    price: float


class Catalogue:
    """The items this process is holding, and the next id to hand out."""

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


def open_catalogue(request: Request) -> Catalogue:
    """Build the catalogue this application shares.

    A dependency is always called with the request, even one that ignores it --
    which is why this is a function rather than `Depends(Catalogue)`.

    **`Depends` goes in the default, never inside `Annotated`.** Every other
    marker -- `Query`, `Body`, `Path`, `Header` -- goes inside `Annotated` so the
    default stays an ordinary Python default; `Depends` is the exception, and
    wreath refuses the other spelling at startup rather than quietly binding the
    parameter from the request body. `scope="app"` is what makes it one
    catalogue for the application instead of a fresh one per request.

    The handler parameters below carry **no annotation**, deliberately: a
    handler's annotations are its wire contract, and `wreath typegen` refuses
    an annotation it cannot render into a client -- which a dependency's type
    is not, since it never crosses the wire.
    """
    return Catalogue()



# `operation_id` names the generated client's method and its React hook --
# `listItems()` and `useListItems()`. Without one the name is derived from the
# method and path (`getItems`, `postItems`), which is fine until two routes
# derive the same one. Naming it here also means renaming the *path* does not
# rename every call site in the front end.


@items.get("/", summary="Every item, newest last", operation_id="listItems")
async def list_items(
    request: Request,
    limit: Annotated[int, Query(minimum=1, maximum=100)] = SETTINGS.page_size,
    catalogue=Depends(open_catalogue, scope="app"),
) -> ItemPage:
    return ItemPage(items=catalogue.all(limit))


@items.get("/{item_id}", summary="One item by id", operation_id="readItem")
async def read_item(
    request: Request,
    item_id: int,
    catalogue=Depends(open_catalogue, scope="app"),
) -> Item:
    item = catalogue.get(item_id)
    if item is None:
        # A wreath exception, so the response is an RFC 9457 problem document
        # rather than `{"detail": ...}`.
        raise NotFound(f"no item {item_id}")
    return item


@items.post("/", status_code=201, summary="Add an item", operation_id="addItem")
async def add_item(
    request: Request,
    body: Annotated[NewItem, Body()],
    catalogue=Depends(open_catalogue, scope="app"),
) -> Item:
    return catalogue.add(body)
'''


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
    return f'''"""The project's own suite, green as delivered.

`asyncio.run` rather than an async test, deliberately: async tests need an async
pytest plugin, and wreath does not install one for you. This needs nothing but
pytest.
"""

from __future__ import annotations

import asyncio

from wreath.testing import TestClient

from {options.name}.app import build


def _drive(scenario):
    """Run one async scenario against a fresh application and its lifespan."""

    async def run():
        async with TestClient(build()) as client:
            await scenario(client)

    asyncio.run(run())


def test_an_empty_catalogue_lists_nothing():
    async def scenario(client):
        response = await client.get("/items")
        assert response.status == 200
        assert response.json() == {{"items": []}}

    _drive(scenario)


def test_an_added_item_comes_back_with_an_id():
    async def scenario(client):
        created = await client.post("/items", json={{"name": "broom", "price": 4.5}})
        assert created.status == 201
        assert created.json()["id"] == 1

        listed = await client.get("/items")
        assert [item["name"] for item in listed.json()["items"]] == ["broom"]

    _drive(scenario)


def test_an_unknown_item_is_a_problem_document_not_a_bare_404():
    """wreath answers RFC 9457, so the body is a document rather than a string.

    Two things worth knowing from this one test: the attribute is `status` and
    not `status_code` (a `TestResponse` is not an httpx response), and `headers`
    is the raw ASGI list of lowercase byte pairs rather than a mapping.

    Asserting the document as well as the code, because a route that had stopped
    being registered at all would also answer 404.
    """

    async def scenario(client):
        response = await client.get("/items/404")
        assert response.status == 404
        assert dict(response.headers)[b"content-type"].startswith(
            b"application/problem+json")
        assert response.json()["status"] == 404

    _drive(scenario)


def test_an_unknown_body_field_is_refused_rather_than_ignored():
    """Extra fields are always rejected -- a client typo is a 422, not a no-op."""

    async def scenario(client):
        response = await client.post(
            "/items", json={{"name": "mop", "price": 3.0, "colour": "red"}})
        assert response.status == 422

    _drive(scenario)
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
        "```",
        "",
        "## Before you deploy",
        "",
        "```bash",
        f"wreath doctor preflight {name}.app:app --environ",
        "```",
        "",
        "One report of everything wreath can check about a built application,",
        "and a named list of what it cannot -- each with the command that does.",
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
            f'psql "$DSN" -c \'CREATE SCHEMA IF NOT EXISTS {name}\'',
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
    return '''{
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
'''


def _web_index_html(options: Options) -> str:
    return f'''<!doctype html>
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
'''


def _web_main(options: Options) -> str:
    return '''import { StrictMode } from "react";
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
'''


def _web_app(options: Options) -> str:
    return f'''// The hooks below come from `web/src/api/`, which is generated from the route
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
'''
