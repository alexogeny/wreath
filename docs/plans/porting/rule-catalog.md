# Rule catalog

Each source construct is classified and, where safe, rewritten. The authoritative
mapping source is the [Coming from FastAPI](../../from-fastapi/index.md) equivalence
tables; this catalog turns those tables into mechanical rules plus a confidence tag.

Tags:

- **1:1** — mechanically rewritten with high confidence.
- **lossy-with-review** — rewritten, but always annotated for a human to confirm.
- **unsupported** — detected and annotated, **never** auto-rewritten.

## App & routing

| Construct | → wreath | Tag |
|---|---|---|
| `FastAPI()` / `APIRouter(prefix=, tags=, dependencies=)` | `Wreath()` / `Router(...)` | 1:1 |
| `@router.get/post/put/patch/delete` | same five decorators | 1:1 |
| `status_code=201` on the decorator | `return JSONResponse(data, status=201)` | lossy |
| `response_model=X` | dropped; return annotation carries the schema | lossy |
| `app.include_router(r, ...)` (static) | `app.include_router(r, ...)` | 1:1 |
| `for ...: app.include_router(...)` (dynamic loop) | — | unsupported (dynamic) |
| `include_in_schema=False` / per-env `docs_url=None` / `create_app()` factory | `app.enable_api_docs(environments=...)` | lossy |

## Request params & binding

| Construct | → wreath | Tag |
|---|---|---|
| handler needs the request | inject `request: Request` as first param | 1:1 |
| `limit: int = Query(20, ge=1, le=100)` | `limit: Annotated[int, Query(minimum=1, maximum=100)] = 20` | 1:1 |
| `Path/Header/Cookie/Form/File` | same markers inside `Annotated`, `alias=` only | 1:1 |
| Pydantic-model body param | dataclass/ORM-typed param, no `Body()` | 1:1 |
| Query string-constraints | no wreath spelling → annotate "move to body model" | lossy |

## Dependencies

| Construct | → wreath | Tag |
|---|---|---|
| `Depends`, `use_cache`, `yield`-cleanup | same semantics; add `request` first arg | 1:1 |
| router `dependencies=get_depends()` (call, not literal) | inline or annotate | lossy |

## Pydantic v2

| Construct | → wreath | Tag |
|---|---|---|
| `class X(BaseModel)` (plain fields) | `@dataclass` | 1:1 |
| `= []` default | `field(default_factory=list)` | 1:1 |
| `model_config = ConfigDict(extra="forbid")` | drop (always-on) | 1:1 |
| `Field(ge=, le=)` on a DTO | `Annotated[T, wreath.binding.Field(ge=, le=)]` | 1:1 |
| `@field_validator` / `@model_validator` | `narrow(...)` / `@rule(...)` | lossy (always annotate) |
| `.model_dump()` in a body | `dataclasses.asdict` | lossy |
| `Model.get_pydantic(include=...)` | hand-written DTO | unsupported |

## ORM models (ormar and SQLModel/SQLAlchemy)

| Construct | → wreath | Tag |
|---|---|---|
| `ormar.Model` + `ormar_config.copy(tablename=)` | `class X(Model, table="x")` | lossy |
| `ormar.UUID(primary_key=, default_factory=)` | `Mapped[UUID] = column(Uuid, primary_key=True, default=...)` | 1:1 |
| scalar column types (`Integer/String/Boolean/DateTime/JSON/UUID`) | `Int64/Varchar/Bool/Timestamp/Jsonb/Uuid` | 1:1 |
| `ge=`, `server_default=` | `check=Ge(...)`, `server_default="..."` | 1:1 |
| **nullability** (ormar nullable) | wreath is NOT NULL by default → make explicit `nullable=True` | lossy (data-integrity policy) |
| `ormar.ForeignKey(X, index=True)` | `column(..., references=X.id)` + `relationship(X, load="raise")` | lossy |
| array column | `column(Array(...))` (see ORM JSONB/array support) | lossy |
| `.objects.filter(...).get_or_none()` / `__` lookups | `session.fetch(X.select().where(...))` | unsupported (detect + annotate) |
| JSONB `jsonb_has_any` / `jsonb_contains` | `.has_any()` / `.contains()` | unsupported (annotate; ties to ORM JSONB support) |

## Alembic

Migration scripts are **not** translated (they hold arbitrary Python and
`postgresql_using` casts). The report emits the command-mapping guidance and a
note that migrations stay in Alembic with `wreath migrations detect/check`
layered on the ported models. **unsupported by design.**

## Middleware / settings / exceptions / lifespan

| Construct | → wreath | Tag |
|---|---|---|
| `add_middleware(CORSMiddleware, ...)` | `configure_http_policy(HttpPolicy(cors=CorsPolicy(...)))` | 1:1 |
| custom `BaseHTTPMiddleware` subclass | copy verbatim; map to a built-in by intent | lossy/unsupported |
| `pydantic-settings` `BaseSettings` (incl. nested groups) | `load_env` + `required_env` + a dataclass | lossy |
| `HTTPException(status_code=<int>, detail=)` | `NotFound(...)` etc. (status→class table) | 1:1 (literal status) |
| `@asynccontextmanager` lifespan | split at `yield` into `@app.on_startup` / `@app.on_shutdown` | lossy |

## No wreath equivalent → surfaced, never faked

GraphQL (`strawberry`), message brokers (`pika`/RabbitMQ), object storage
(`boto3`), SMS (`twilio`), feature flags (Unleash), advisory locks
(`sqlalchemy-dlock`, partial), Celery, Sentry, and private-registry packages are
emitted verbatim and flagged `unsupported`, with a pointer to the relevant native
wreath subsystem where one exists.
