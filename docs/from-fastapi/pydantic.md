# Pydantic and validation

Pydantic gives FastAPI its request bodies, its constraint vocabulary, its error
lists, and — through `BaseSettings` — its configuration. Wreath covers the same
ground without Pydantic: bodies are dataclasses or ORM models, validators are
compiled once at application startup, and constraints live where they can be
enforced end to end. This page maps each habit onto its Wreath home. One thing
to know up front: a Pydantic model annotation is not accepted by Wreath's
binder — the translations below are the supported path.

## `BaseModel` → dataclass

Where you would declare a `BaseModel` for a request body, declare a dataclass.
It is auto-detected as the body from the annotation alone, exactly as in
FastAPI — no `Body()` marker needed:

=== "FastAPI"

    ```python
    from pydantic import BaseModel

    class NewItem(BaseModel):
        name: str
        price: int
        tags: list[str] = []

    @app.post("/items")
    async def create(item: NewItem):
        ...
    ```

=== "Wreath"

    ```python
    from dataclasses import dataclass, field

    @dataclass
    class NewItem:
        name: str
        price: int
        tags: list[str] = field(default_factory=list)

    @app.post("/items")
    async def create(request: Request, item: NewItem) -> dict:
        ...
    ```

Dataclasses nest recursively, and fields may be scalars, `Any`, `None`,
`list[T]`, `tuple[T, ...]`, `dict[str, T]`, and optional unions. The validator
for each dataclass is generated when the application starts, so request-time
validation is a single compiled pass rather than per-request reflection.

The defaults are strict in the places Pydantic v2 made configurable:

- **Unknown fields are rejected** — the behaviour of
  `model_config = ConfigDict(extra="forbid")`, always on. The error type is
  `"extra"`.
- **`bool` is never coerced to `int`**, and strings are never parsed into
  numbers. A JSON number is accepted for a `float` field; that is the extent of
  the coercion.
- **Malformed JSON is a `400`**, before validation begins; a well-formed body
  with wrong shapes is a `422`.

There is no `model_dump()` on the way out because there is no output model:
return a `dict` (or a response object) from the handler. `dataclasses.asdict`
covers the case where you want to echo a validated body back.

## `Field(ge=..., le=...)` → constraints where they belong

Pydantic puts constraints on the model field. Wreath splits them by where they
can actually be enforced.

**Query parameters** take numeric bounds through the `Query` marker, inside
`Annotated`, with the default remaining an ordinary Python default:

=== "FastAPI"

    ```python
    @app.get("/search")
    async def search(limit: int = Query(20, ge=1, le=100)):
        ...
    ```

=== "Wreath"

    ```python
    from typing import Annotated
    from wreath.binding import Query

    @app.get("/search")
    async def search(
        request: Request,
        limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
    ) -> dict:
        ...
    ```

`minimum`/`maximum` apply to `int` and `float` parameters, and
`overflow="clamp"` pins an out-of-range value to the nearest bound instead of
rejecting it — useful for pagination, where `limit=99999` usually means "as
many as you'll give me", not "please fail". String constraints on query
parameters don't exist; a value constrained enough to need them belongs in the
body, validated by a model.

**Model data** takes its constraints on the ORM column, where the same rule
guards both the API and the database:

```python
from wreath.orm import Mapped, Model, column
from wreath.orm.constraints import Ge, Length, OneOf
from wreath.orm.types import Int64, Text

class Employee(Model, table="employees"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, check=Length(1, 200))
    salary: Mapped[int] = column(Int64, check=Ge(0))
    grade: Mapped[str] = column(Text, check=OneOf("intern", "junior", "senior"))
```

The vocabulary is `Ge`, `Gt`, `Le`, `Lt`, `Length(minimum, maximum)`,
`Pattern`, `OneOf(*values)`, and `Predicate` for an arbitrary function. Annotate
a handler parameter with the model and the body is validated against exactly
these rules — one definition, one enforcement, no drift between your API schema
and your table. The [ORM page](sqlmodel.md) picks this up in full.

## `@field_validator` and `@model_validator` → rules

Pydantic's validator decorators become explicit rules on the model.
`narrow` tightens one field beyond its column check; `@rule` validates across
fields, the way a `@model_validator(mode="after")` would:

```python
from wreath.orm.constraints import Le, narrow, rule

class Intern(Employee, table="interns"):
    salary_cap = narrow("salary", Le(50_000))

    @rule("salary", "tenure_months", at="salary")
    def pay_band(salary: int, tenure_months: int) -> bool:
        """an intern past six months cannot be paid more than 40k"""
        return not (tenure_months > 6 and salary > 40_000)
```

A failed rule reports at the field named by `at=`, with the docstring as its
message. For validation that doesn't belong to a model at all — cross-request
checks, lookups — write plain code in the handler or a dependency and raise the
appropriate exception; there is no hook system to learn.

## The error shape

Both frameworks report every problem at once, each with a location, a message,
and a type. The envelope differs: FastAPI wraps errors in `{"detail": [...]}`,
while Wreath answers RFC 9457 `application/problem+json`:

=== "FastAPI"

    ```json
    {
      "detail": [
        {"loc": ["body", "price"], "msg": "Input should be a valid integer",
         "type": "int_parsing"}
      ]
    }
    ```

=== "Wreath"

    ```json
    {
      "type": "about:blank",
      "title": "Unprocessable Entity",
      "status": 422,
      "detail": "Request validation failed",
      "errors": [
        {"loc": ["body", "price"], "msg": "value is not an integer", "type": "int"}
      ]
    }
    ```

The `loc` convention carries over directly. Type names are Wreath's own —
`missing`, `int`, `float`, `bool`, `str`, `list`, `dict`, `union`, `null`,
`extra`, `minimum`, `maximum` — so update any client or test asserting on
Pydantic's. Every Wreath error response uses this problem+json shape, not only
validation.

## Settings

`BaseSettings` bundles environment parsing, type conversion, and defaults into
a model. Wreath [keeps configuration and state apart](../guides/config-state.md)
and keeps environment reading deliberately literal:

```python
from wreath.config import load_env
from wreath.server import run

env = load_env(".env", apply=True)   # strict KEY=value; no expansion, no quoting rules
run(app, required_env=["DATABASE_URL", "SECRET_KEY"])
```

Server settings bind from `WREATH_*` variables (`WREATH_HOST`, `WREATH_PORT`,
and the rest — `wreath run --help` lists them), and `required_env` names the
variables you cannot boot without, so a missing secret is a warning at startup
rather than a failure on the first request. There is no settings class to
subclass; read the environment, keep the values where you need them, and let
`app.state` hold what the application builds from them.

## Quick reference

| Pydantic habit | Wreath home |
|---|---|
| `class X(BaseModel)` body | `@dataclass` body, auto-detected from the annotation |
| Model that mirrors a table | One `wreath.orm` model doing both jobs |
| `Field(ge=, le=)` on query input | `Annotated[int, Query(minimum=, maximum=, overflow=)]` |
| `Field` constraints on model data | `column(..., check=Ge(...) / Length(...) / Pattern(...) / OneOf(...))` |
| `@field_validator` | `narrow("field", ...)` or plain handler code |
| `@model_validator` | `@rule("a", "b", at="a")` |
| `extra="forbid"` | Always on |
| `model_dump()` | Return a `dict`; `dataclasses.asdict` if echoing a body |
| `{"detail": [...]}` errors | RFC 9457 problem+json with an `errors` list |
| `BaseSettings` | `wreath.config.load_env` + `WREATH_*` + `required_env` |
