---
description: Declare PostgreSQL models, bind request-scoped sessions and keep raw SQL safe.
keywords: guide PostgreSQL ORM models sessions transactions SQL migrations schema
---

# PostgreSQL and models

The database, model registry and request session have different owners. Register each
once at startup; bind the session by database and workload at the handler boundary.

```python title="app.py"
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.config import Environment, read_osenv
from wreath.orm import FromORM, Mapped, Model, Session, column
from wreath.orm.types import Int64, Text


class Project(Model, table="projects"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, unique=True)
    state: Mapped[str] = column(Text, default="active")


ReadSession = Annotated[Session, FromORM("main", workload="read")]
WriteSession = Annotated[Session, FromORM("main", workload="write")]


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://localhost/wreath_dev"


settings = Environment(read_osenv()).bind(Settings)
app = Wreath()
app.postgres(
    "main",
    dsn=settings.database_url,
    pools={
        "read": {"min_size": 1, "max_size": 8},
        "write": {"min_size": 1, "max_size": 4},
    },
)
app.orm(database="main", models=[Project])


@app.get("/projects")
async def list_projects(request: Request, db: ReadSession, limit: int = 20) -> list[dict]:
    query = Project.select().order_by(Project.id).limit(min(limit, 100))
    rows = await db.fetch(query)
    return [{"id": row.id, "name": row.name, "state": row.state} for row in rows]


@app.post("/projects")
async def create_project(request: Request, project: Project, db: WriteSession) -> dict:
    db.add(project)
    await db.flush()
    return {"id": project.id, "name": project.name, "state": project.state}
```

The model is also a validated request body. Unknown fields and invalid types are 422s
before a connection is leased. `flush()` owns one transaction and the request-scoped
session is returned after the response body finishes.

```python title="test_model.py"
from app import Project


def test_the_model_contract_is_compiled_at_declaration() -> None:
    project = Project(name="migration dashboard")
    assert project.name == "migration dashboard"
    assert project.state == "active"
    assert [column.python_name for column in Project.__wreath_columns__] == [
        "id",
        "name",
        "state",
    ]
```

Use expressions for data and allowlisted fragments for SQL syntax. Never interpolate a
request value into an ordinary string:

```python title="safe_sql.py"
from wreath.orm import Session
from wreath.sql import Fragment, Identifier

DIRECTIONS = {"asc": "ASC", "desc": "DESC"}


async def search(db: Session, organization_id: str, term: str, direction: str):
    order = Fragment(DIRECTIONS[direction])
    pattern = f"%{term}%"
    return await db.raw(
        t"SELECT id, name FROM {Identifier('public', 'projects')} "
        t"WHERE organization_id = {organization_id} AND name ILIKE {pattern} "
        t"ORDER BY id {order}"
    ).fetch()
```

Generate Wreath-owned support-table DDL for review with
`uv run wreath schema sql app:app`. Application model changes use immutable migration
artifacts rather than silent startup mutation. Follow the complete
[detect, baseline, generate, review, apply and rollback workflow](migration-workflow.md), then
use the [data and analysis API](../reference/data.md) for individual declarations.
