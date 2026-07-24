"""The translation rule catalog (design 07 §2), as a flat registry.

Each rule maps a recognized source construct to (construct-name, coverage-category,
verdict-tag, message). The message names the wreath target idiom or the reason a
site needs review. Rule ids are stable and appear in the report so a reviewer can
`grep` a worklist. Seeded from docs/from-fastapi/{index,pydantic,sqlmodel,alembic}.md.
"""
from __future__ import annotations

from .ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED

# rule_id -> (construct, category, tag, message)
RULES: dict[str, tuple[str, str, str, str]] = {
    # -- routing --------------------------------------------------------------
    "route.app": ("app", "routing", TRANSLATED, "FastAPI() -> Wreath()"),
    "route.router": ("router", "routing", TRANSLATED, "APIRouter(prefix=, tags=) -> Router(prefix=, tags=)"),
    "route.method": ("route", "routing", TRANSLATED, "@router.<method> maps 1:1; handler gains a `request: Request` param"),
    "route.include_static": ("include_router", "routing", TRANSLATED, "include_router() maps 1:1"),
    "route.include_dynamic": ("include_router", "routing", UNSUPPORTED, "dynamic include_router in a loop — static analysis cannot unroll; wire routers explicitly"),
    "route.websocket": ("route", "routing", NEEDS_REVIEW, "WebSocket handler: wreath registers WS via @app.websocket (Router has no .websocket) — move a router-level handler to the app; WebSocket/WebSocketDisconnect import from wreath.websocket; path params via ws.path_params"),
    "ws.json_method": ("websocket", "other", NEEDS_REVIEW, "WebSocket.send_json/receive_json has no wreath equivalent — use send_text/receive_text with json.dumps/loads"),
    # -- route options (not floor-checked; counted in overall) ----------------
    "route.response_model": ("route_option", "other", TRANSLATED, "response_model -> drop the kwarg; the handler return annotation is wreath's schema source (runtime response-filtering is not replicated)"),
    "route.status_code": ("route_option", "other", NEEDS_REVIEW, "status_code on a multi-statement handler -> wrap the success return in JSONResponse(..., status=) by hand"),
    "route.status_code_return": ("route_option", "other", TRANSLATED, "status_code on a single-return handler -> return JSONResponse(<expr>, status=<int>)"),
    "route.include_in_schema": ("route_option", "other", NEEDS_REVIEW, "include_in_schema=False -> exclude from app.enable_docs() surface"),
    # -- params ---------------------------------------------------------------
    "param.query": ("param", "params", TRANSLATED, "Query(default, ge=, le=) -> Annotated[T, Query(minimum=, maximum=)] = default"),
    "param.query_strconstraint": ("param", "params", NEEDS_REVIEW, "Query string-constraint (min_length/regex) has no wreath scalar slot; move to a body model"),
    "param.path": ("param", "params", TRANSLATED, "Path(...) -> Annotated[T, Path()]"),
    "param.header": ("param", "params", TRANSLATED, "Header(...) -> Annotated[T, Header(alias=)]"),
    "param.cookie": ("param", "params", TRANSLATED, "Cookie(...) -> Annotated[T, Cookie()]"),
    "param.form": ("param", "params", TRANSLATED, "Form(...) -> Annotated[T, Form()]"),
    "param.file": ("param", "params", TRANSLATED, "File()/UploadFile -> Annotated[UploadFile, File()]"),
    "param.body": ("param", "params", TRANSLATED, "Pydantic-typed body param -> dataclass/ORM-typed param (no Body())"),
    # -- pydantic models ------------------------------------------------------
    "pydantic.model": ("model", "pydantic_models", TRANSLATED, "class X(BaseModel) -> @dataclass"),
    "pydantic.field": ("field", "pydantic_models", TRANSLATED, "plain field maps 1:1 (list default -> field(default_factory=list))"),
    "pydantic.field_constraint": ("field", "pydantic_models", NEEDS_REVIEW, "Field(ge=/le=) on a DTO has no dataclass slot; move to a model check or handler guard"),
    # -- pydantic extras (not floor-checked) ----------------------------------
    "pydantic.config_forbid": ("config", "other", TRANSLATED, "extra='forbid' is always-on in wreath; drop it"),
    "pydantic.config_ignore": ("config", "other", NEEDS_REVIEW, "extra='ignore' has no wreath equivalent (wreath always forbids extras)"),
    "pydantic.config_class": ("config", "other", NEEDS_REVIEW, "pydantic v1 `class Config` — remove it (wreath forbids extras by default; any other Config options are manual)"),
    "pydantic.validator": ("validator", "other", NEEDS_REVIEW, "field_validator/model_validator -> narrow()/@rule() with custom logic (manual)"),
    "pydantic.get_pydantic": ("get_pydantic", "other", UNSUPPORTED, "Model.get_pydantic() metaprogramming -> a hand-written DTO / column subset"),
    # -- dependencies ---------------------------------------------------------
    "depends.use": ("depends", "dependencies", TRANSLATED, "Depends(...) maps 1:1; the dependency callable gains a `request` param"),
    "depends.router_call": ("depends_wiring", "other", NEEDS_REVIEW, "router dependencies=<call> is not a literal list; inline the Depends(...)"),
    # -- ORM models -----------------------------------------------------------
    "orm.model": ("orm_model", "orm_models", TRANSLATED, "ormar.Model/SQLModel -> wreath.orm.Model(table=...)"),
    "orm.column": ("column", "orm_models", TRANSLATED, "column type maps via the ORM table (note: wreath is NOT NULL by default — verify nullability)"),
    "orm.fk": ("column", "orm_models", NEEDS_REVIEW, "ForeignKey -> column(references=) + relationship(load='raise'); FK column type unresolved (referenced PK not found in-module) — set it by hand"),
    "orm.fk_typed": ("column", "orm_models", TRANSLATED, "ForeignKey -> column(<PK type inferred from the referenced model>, references=) + relationship(load='raise')"),
    # -- exceptions -----------------------------------------------------------
    "exc.http_literal": ("httpexception", "exceptions", TRANSLATED, "HTTPException(status_code=<int>) -> the matching wreath exception class"),
    "exc.http_variable": ("httpexception", "exceptions", NEEDS_REVIEW, "HTTPException with a non-literal status_code -> map by hand"),
    "exc.handler": ("exception_handler", "exceptions", TRANSLATED, "@app.exception_handler(...) maps 1:1"),
    # -- settings -------------------------------------------------------------
    "settings.class": ("settings", "settings", NEEDS_REVIEW, "BaseSettings class -> load_env + a plain dataclass (map env names/defaults by hand)"),
    "settings.field": ("settings", "settings", TRANSLATED, "scalar env field -> required_env()/load_env() lookup"),
    "settings.nested": ("settings", "settings", NEEDS_REVIEW, "composed BaseSettings sub-group -> flatten by hand"),
    # -- queries (the annotate-only tar-pit) ----------------------------------
    "orm.query": ("orm_query", "queries", UNSUPPORTED, "ormar .objects. query -> session.fetch(Model.select().where(...)); rewrite by hand (design 07 §6)"),
    # -- middleware / lifespan / infra (not floor-checked) --------------------
    "mw.cors": ("middleware", "other", TRANSLATED, "add_middleware(CORSMiddleware, ...) -> add_middleware(CORSMiddleware(...)) (instance form)"),
    "mw.trustedhost": ("middleware", "other", TRANSLATED, "TrustedHostMiddleware -> wreath security middleware (instance form)"),
    "mw.custom": ("middleware", "other", NEEDS_REVIEW, "custom BaseHTTPMiddleware -> wreath's fused middleware base (rework); check built-ins first"),
    "lifespan.ctx": ("lifespan", "other", NEEDS_REVIEW, "@asynccontextmanager lifespan -> split at yield into @app.on_startup / @app.on_shutdown"),
    # Now portable to a SHIPPED wreath subsystem (was unsupported): reviewable, not
    # auto-translatable (the task/loop body is bespoke) — needs-review with a real target.
    "bg.celery": ("background", "other", NEEDS_REVIEW, "Celery task -> wreath jobs: app.jobs()/@jobs.task + jobs.schedule(cron=) (built); port the task body by hand"),
    "bg.asyncio_loop": ("background", "other", NEEDS_REVIEW, "asyncio background loop -> a supervised wreath service or app.jobs() (built)"),
    "graphql.mount": ("graphql", "other", UNSUPPORTED, "GraphQL/strawberry server has no wreath equivalent (deliberately out of scope)"),
    "ext.boto3": ("external", "other", UNSUPPORTED, "boto3/AWS SDK is not a framework feature; keep the external library"),
    "ext.aiometer": ("external", "other", NEEDS_REVIEW, "aiometer/tenacity outbound throttle+retry -> app.http_client(rate=, retries=) (built)"),
    "ext.s3path": ("external", "other", NEEDS_REVIEW, "s3path.S3Path -> wreath.storage Storage/StoragePath (built, design 09)"),
    "ext.gql": ("external", "other", UNSUPPORTED, "gql GraphQL client has no wreath equivalent; keep the external library"),
    "form.as_form": ("form_binding", "other", TRANSLATED, "as_form decorator deleted; consuming `Depends(Model.as_form)` -> `Annotated[Model, Form()]` whole-model multipart binding (built)"),
    "lock.dlock": ("advisory_lock", "other", NEEDS_REVIEW, "sqlalchemy-dlock -> wreath advisory locks db.lock()/db.try_lock()/Session.lock() (built, design 03)"),
    "auth.jwt": ("auth", "other", NEEDS_REVIEW, "manual JWT/JWKS verify -> app.oidc_provider()/BearerTokenBackend/JwtVerifier (built, design 02)"),
    "auth.oauth": ("auth", "other", NEEDS_REVIEW, "authlib OAuth2/client-credentials -> wreath oauth2_login()/ClientCredentials (built, design 02)"),
    "mig.manual": ("migration_op", "other", UNSUPPORTED, "Alembic alter_column(postgresql_using=...) is a MANUAL op; keep Alembic (design 07 Alembic posture)"),
    # -- confidence -----------------------------------------------------------
    "resolve.star_import": ("star_import", "other", NEEDS_REVIEW, "`from x import *` reduces name-resolution confidence for this module"),
}


def rule(rule_id: str) -> tuple[str, str, str, str]:
    return RULES[rule_id]
