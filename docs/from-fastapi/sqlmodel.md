# SQLModel, SQLAlchemy, and the ORM

SQLModel's promise is one class that is both your table and your API schema.
`wreath.orm` keeps that promise — a model describes a table *and* validates
request bodies bound to it — and pairs it with a different set of defaults:
types are named explicitly, relationships never query behind your back, and the
ORM refuses to manage your schema at all. This page walks the familiar
SQLAlchemy/SQLModel surface — declaration, sessions, queries, relationships —
and shows each piece's Wreath form.

One scoping honesty first: `wreath.orm` sits on Wreath's native
[PostgreSQL driver](../guides/postgres.md), and PostgreSQL is the only backend.
There is no dialect layer, no SQLite mode for tests (the test client plus a real
PostgreSQL is the intended combination), and no plan to hide that.

## Declaring a model

=== "SQLModel"

    ```python
    from sqlmodel import Field, Relationship, SQLModel

    class Team(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class Hero(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        age: int | None = None
        team_id: int | None = Field(default=None, foreign_key="team.id")
        team: Team | None = Relationship(back_populates="heroes")
    ```

=== "Wreath"

    ```python
    from wreath.orm import Mapped, Model, column, relationship
    from wreath.orm.types import Int64, Text

    class Team(Model, table="teams"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)

    class Hero(Model, table="heroes"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        age: Mapped[int] = column(Int64, nullable=True)
        team_id: Mapped[int] = column(Int64, nullable=True, references=Team.id)
        team = relationship(Team, foreign_key=team_id, load="raise")
    ```

The differences are all deliberate:

- **The table name is written down** (`table="heroes"`), never derived from the
  class name. A class kwarg-less `Model` subclass is a reusable mixin, not a
  table — and inheriting from a mapped model is an error, so a table cannot
  acquire columns by accident.
- **Every column names its PostgreSQL type** — `Int64`, `Int32`, `Text`,
  `Varchar`, `Bool`, `Float64`, `Uuid`, `Date`, `Timestamp`, `TimestampTz`,
  `Jsonb`, and the rest of [`wreath.orm.types`](../reference/orm.md). Nothing is
  inferred from the Python annotation; `Mapped[int]` is documentation for your
  editor, and the registry compiles from the `column()` declarations alone.
- **Columns are `NOT NULL` unless you say `nullable=True`** — the reverse of
  SQLAlchemy's default, and the safer one to forget.
- **Foreign keys point at columns, not strings** — `references=Team.id`, which
  your editor can follow and refactor.
- **`relationship` declares its loading strategy** — `"raise"` (the default),
  `"selectin"`, or `"joined"`. More on why below.

Constraints (`check=Ge(0)`, `Length`, `Pattern`, `OneOf`) and cross-field rules
live on the model too, and the same model validates request bodies bound to it —
that story is on the [Pydantic page](pydantic.md#field_validator-and-model_validator-rules).

## Engine and session → registry and session

Where SQLAlchemy has `create_engine` + `Session(engine)`, Wreath wires the
database and models onto the application, and hands sessions to handlers:

```python
app = Wreath()
app.postgres("main", dsn="postgres://app@localhost/app")
app.orm(database="main", models=[Team, Hero])
```

A handler asks for a session with an annotation, the way you'd write a
`Depends(get_session)` today — except the request-scoped lifecycle you build in
FastAPI with a `yield` dependency is the built-in behaviour:

=== "SQLModel"

    ```python
    def get_session():
        with Session(engine) as session:
            yield session

    @app.get("/heroes")
    def heroes(session: Session = Depends(get_session)):
        return session.exec(select(Hero)).all()
    ```

=== "Wreath"

    ```python
    from typing import Annotated
    from wreath.orm import FromORM, Session

    @app.get("/heroes")
    async def heroes(
        request: Request,
        session: Annotated[Session, FromORM("main", workload="read")],
    ) -> dict:
        heroes = await session.fetch(Hero.select())
        return {"heroes": [{"id": h.id, "name": h.name} for h in heroes]}
    ```

The session is lazy — no connection leaves the pool until the first statement —
and it is closed for you when the request ends. `workload=` names which of the
database's pools it draws from, so read traffic and write traffic can be
separated without ceremony. A bare `Session` annotation is a startup error, not
a silent global: the annotation must say which database it means.

## Queries

Queries hang off the model rather than a freestanding `select()`, and the
builder vocabulary transfers almost verbatim:

=== "SQLModel"

    ```python
    statement = (
        select(Hero)
        .where(Hero.age >= 18, Hero.name.like("A%"))
        .order_by(Hero.name)
        .limit(10)
    )
    heroes = session.exec(statement).all()
    hero = session.get(Hero, 1)
    ```

=== "Wreath"

    ```python
    query = (
        Hero.select()
        .where(Hero.age >= 18, Hero.name.like("A%"))
        .order_by(Hero.name)
        .limit(10)
    )
    heroes = await session.fetch(query)
    hero = await session.get(Hero, 1)
    ```

`.where()` ANDs its predicates; combine with `&`, `|`, `~` (or `and_`, `or_`,
`not_`) when you need more. Column operators include the comparisons plus
`.like()`, `.ilike()`, `.in_()`, `.not_in()`, `.is_null()`, `.is_not_null()`.
`session.fetch_one()` returns one row or `None` and raises on more than one;
`.for_update()` exists and insists on a write-workload session inside an
explicit transaction. `Hero.select(Hero.id, Hero.name)` selects a subset of
columns — and reading a column you did not select raises rather than returning
a stale or lazy value.

Raw SQL never became a second-class citizen:

```python
rows = await session.raw(
    "select id, name from heroes where age >= $1", 18
).models(Hero)
```

## Writes and transactions

`add`, `delete`, and dirty tracking work as your fingers expect; `flush` writes
pending changes, and outside an explicit transaction it commits atomically on
its own:

```python
hero = Hero(name="Ada", age=36, team_id=None)
session.add(hero)
await session.flush()          # its own transaction: commit or rollback, atomically

async with session.begin():    # explicit transaction; nesting makes savepoints
    hero.name = "Ada L."       # plain assignment marks the field dirty
    session.delete(other)
    await session.flush()
```

There is no `session.commit()` to forget: `begin()` commits when the block
exits cleanly and rolls back when it doesn't, and a bare `flush()` is atomic on
its own. Flush order is deterministic — inserts, then updates, then deletes.

## Relationships: nothing loads implicitly

SQLAlchemy's default lazy loading turns an attribute read into a query — the
classic N+1, invisible until production. Wreath's default is `load="raise"`:
reading a relationship you did not load raises `UnloadedRelationshipError`
instead of querying. You say how to load, in the query or after it:

```python
# in the query: batched IN (...) load, or a join
heroes = await session.fetch(Hero.select().include(Hero.team.selectin()))
posts = await session.fetch(Post.select().include(Post.author.joined()))

# after the fact, batched across the whole list
users = await session.fetch(User.select())
await session.load(users, User.posts)
```

Declaring `load="selectin"` or `load="joined"` on the `relationship()` makes
that strategy the default for every query of the model. Either way, the query
count is decided where the query is written — never by an attribute access
three files away.

## There is no `create_all`

`SQLModel.metadata.create_all(engine)` has no Wreath equivalent, on purpose.
The ORM never creates, alters, or drops anything. At startup it *validates*
that the live schema matches the models — `app.orm(...,
validate_schema="error")` is the default — and tells you precisely where they
disagree. Your schema is managed by your migration tool, with its own
credentials and its own deploy step; today that tool should remain Alembic,
and the [Alembic page](alembic.md) explains exactly which parts Wreath already
replaces.

## Quick reference

| SQLAlchemy / SQLModel | Wreath |
|---|---|
| `class Hero(SQLModel, table=True)` | `class Hero(Model, table="heroes")` |
| `Field(primary_key=True)` | `column(Int64, primary_key=True)` |
| `Field(foreign_key="team.id")` | `column(Int64, references=Team.id)` |
| Nullable by default | `NOT NULL` by default; `nullable=True` to opt out |
| `Relationship(back_populates=...)` | `relationship(Team, foreign_key=team_id, load=...)` |
| `create_engine(url)` + `Session(engine)` | `app.postgres("main", dsn=...)` + `app.orm(database="main", models=[...])` |
| `Depends(get_session)` with `yield` | `Annotated[Session, FromORM("main", workload="read")]` |
| `select(Hero).where(...)` | `Hero.select().where(...)` |
| `session.exec(stmt).all()` | `await session.fetch(query)` |
| `session.get(Hero, 1)` | `await session.get(Hero, 1)` |
| `selectinload` / `joinedload` | `.include(Hero.team.selectin())` / `.joined()`, or `load=` on the relationship |
| Lazy loading on attribute read | Raises `UnloadedRelationshipError`; loading is always explicit |
| `session.add` / `delete` / dirty tracking | Same, with `await session.flush()` |
| `session.begin()` / savepoints | `async with session.begin():`, nested blocks are savepoints |
| `session.execute(text(sql))` | `session.raw(sql, *args)` — `.fetch()`, `.fetchval()`, `.models(Hero)` |
| `metadata.create_all` | Nothing — startup validation only; DDL belongs to migrations |
