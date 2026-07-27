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
    # `status_code=` has no wreath slot: the status lives on the response the
    # handler returns. Whether that is a mechanical change depends on what the
    # handler returns, and for a *literal* return wreath's own coercion table
    # decides it — `coerce_json` for a dict/list/number, `coerce_text` for a str
    # (app._to_response). Wrapping such a return in the class wreath would have
    # picked anyway changes the status and nothing else, which is why these are
    # determined and a `return some_name` is not.
    "route.status_code": ("route_option", "other", NEEDS_REVIEW, "status_code -> the status belongs on the response the handler returns, and this return's runtime type is not visible here: wreath picks the response class by type (dict/list/number -> JSONResponse, str -> TextResponse, a dataclass needs dataclasses.asdict first), so choose the wrapper that matches what this actually returns"),
    "route.status_code_return": ("route_option", "other", TRANSLATED, "status_code with one return of a JSON literal -> return JSONResponse(<expr>, status=<int>) and drop the kwarg; wreath already routes a dict/list/number return through JSONResponse, so only the status changes"),
    "route.status_code_text": ("route_option", "other", TRANSLATED, "status_code with one return of a str literal -> return TextResponse(<expr>, status=<int>) and drop the kwarg; a str return is text/plain in wreath, so JSONResponse would change the content type as well as the status"),
    "route.status_code_response": ("route_option", "other", TRANSLATED, "status_code on a handler that already returns a response object -> drop the route kwarg and pass status= to that response (fastapi's status_code= becomes wreath's status=); the route-level value was already dead, since the returned response's own status wins"),
    "route.status_code_empty": ("route_option", "other", TRANSLATED, "status_code=204/304 on a handler with no return -> return Response(status=<int>); wreath coerces a bare `None` return to a 200 JSON `null`, and Response omits content-length for a bodiless status"),
    "route.status_code_empty_body": ("route_option", "other", NEEDS_REVIEW, "status_code=204/304 but the handler returns a value — a bodiless status with a body is a contradiction the source got away with; decide which one is wrong before porting it"),
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
    "param.body_embed": ("param", "params", NEEDS_REVIEW, "Body(..., embed=True) wraps the value in a single-key object and wreath has no switch for that — give the DTO the wrapping field, or drop `embed` and change the client"),
    # -- pydantic models ------------------------------------------------------
    "pydantic.model": ("model", "pydantic_models", TRANSLATED, "class X(BaseModel) -> @dataclass"),
    "pydantic.field": ("field", "pydantic_models", TRANSLATED, "plain field maps 1:1 (list default -> field(default_factory=list))"),
    "pydantic.field_constraint": ("field", "pydantic_models", NEEDS_REVIEW, "Field(ge=/le=) on a DTO has no dataclass slot — wreath's `Body`/`Form` markers carry only `alias`, so the constraint has three possible homes and they do not behave alike: `column(..., check=Ge(...))` if this DTO mirrors a table (guards the API and the database, one definition), `Annotated[int, Query(minimum=, maximum=)]` if the value is really a scalar parameter, or a handler guard raising UnprocessableEntity. Only the first two keep it a 422 at the boundary"),
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
    #
    # Split by *field shape*, the same way `.objects.filter()` is split by
    # argument shape. A `BaseSettings` class of plain scalars with literal
    # defaults is a mechanical rewrite: pydantic-settings' default source reads
    # the field name (case-insensitively, so upper-case is the canonical
    # spelling) with `env_prefix` in front, no default means required, and
    # `str`/`int`/`float`/`bool` are the four conversions `load_env`'s
    # `dict[str, str]` needs. It stops being mechanical at a validator, a
    # container type, a computed default, or a sub-group — so the class-level
    # verdict is "every field is mechanical", not "it is a BaseSettings".
    "settings.class": ("settings", "settings", NEEDS_REVIEW, "BaseSettings class -> load_env + a plain dataclass; this one is not field-by-field mechanical, so map the env names and defaults by hand"),
    "settings.class_env": ("settings", "settings", TRANSLATED, "BaseSettings of plain scalars -> `env = load_env('.env', apply=True)` plus a @dataclass whose fields read `env[<PREFIX><FIELD_NAME upper-cased>]`, with each literal default as the dataclass default and each field that has none listed in `run(app, required_env=[...])`"),
    "settings.field": ("settings", "settings", TRANSLATED, "scalar env field -> one `env[...]` lookup: `str` verbatim, `int`/`float` through the constructor, and `bool` through `value.lower() in {'1','true','t','yes','y','on'}` — pydantic-settings' own truthy set, spelled out because `load_env` returns strings and `bool('false')` is True"),
    "settings.field_complex": ("settings", "settings", NEEDS_REVIEW, "env field with a container/optional type, a Field(...) marker or a computed default -> pydantic-settings would JSON-decode or build this value; `load_env` hands you the raw string, so the parse is yours to write"),
    "settings.nested": ("settings", "settings", NEEDS_REVIEW, "composed BaseSettings sub-group: the sub-group's own fields still read their own env names, so the *values* carry across, but two things do not — pydantic-settings will also accept the whole group as one JSON object (and, with env_nested_delimiter set, as `PARENT__CHILD`), and flattening changes every `settings.<group>.<field>` read. Decide whether the port keeps the group as a nested dataclass or flattens it"),
    # -- queries ---------------------------------------------------------------
    #
    # `.objects.` is the largest single construct in a real ormar codebase — a
    # third of every framework token in the corpus this catalog was measured
    # against. One generic verdict for all of it reports the *size* of the job
    # and nothing about its *shape*, so each verb names the call it becomes.
    #
    # The split within a verb is by *argument*, not by verb alone. `filter(id=x)`
    # is a mechanical rewrite — every keyword maps to a wreath predicate with the
    # value carried across untouched. `filter(name__icontains=x)` is not: the
    # value has to be wrapped in wildcards. `filter(ranch__slug=x)` is not
    # either — but *not* because the join is a decision, which it is not:
    # `Model.ranch.slug` is a `RelatedColumnExpr` and `plan_filter_joins` emits
    # the INNER JOIN itself, choosing INNER because a parent with no matching
    # child cannot satisfy a predicate on the child's column. The real blocker
    # is resolution: turning `ranch__slug` into `Model.ranch.slug` means knowing
    # `ranch` is a relation and `slug` a column on its target, and a model is
    # usually declared in a different module from the query. `analyze` has a
    # tree-wide index and could; `emit_module` is per-module and takes raw source
    # text, so it could not — and `query_rule` is shared precisely so the report
    # and the emitted TODO cannot disagree. Promoting this needs the emitter to
    # gain a tree-wide index first. Same verb, three verdicts, and the argument
    # list is what tells them apart — so the analyzer reads it rather than
    # guessing from the name.
    "orm.query": ("orm_query", "queries", UNSUPPORTED, "ormar .objects. query -> session.fetch(Model.select().where(...)); rewrite by hand (design 07 §6)"),
    "orm.query.filter": ("orm_query", "queries", NEEDS_REVIEW, "filter(**kw) -> Model.select().where(Model.col == value); run it with session.fetch() for a list. This one needs a decision: a `__icontains`/`__startswith` lookup rewrites the *value* (wrap it in wildcards for .ilike()), a `__isnull` has no negated form, a relation lookup (`owner__name`) is `Model.owner.name` — wreath plans that INNER JOIN itself, but resolving `owner` to its target model is cross-module and this tool works one module at a time — and a jsonb lookup needs the container operator by hand"),
    "orm.query.filter_exact": ("orm_query", "queries", TRANSLATED, "filter(**kw) -> Model.select().where(Model.col == value, ...) — every keyword here maps to a wreath predicate with the value unchanged (`__gte` -> >=, `__in` -> .in_()). Run it with session.fetch() for a list, session.count() for a count"),
    "orm.query.get_or_none": ("orm_query", "queries", NEEDS_REVIEW, "get_or_none(**kw) -> await session.fetch_one(Model.select().where(...)) — same contract, None on no match. This call's lookups do not map straight across; see the filter note"),
    "orm.query.get_or_none_exact": ("orm_query", "queries", TRANSLATED, "get_or_none(**kw) -> await session.fetch_one(Model.select().where(...)) — the contract matches exactly: None on no match, and both raise when more than one row matches"),
    "orm.query.get": ("orm_query", "queries", NEEDS_REVIEW, "get(pk) -> await session.get(Model, pk); get(**kw) -> session.fetch_one(...). Left for review even when the arguments are simple, because the *miss* changes: ormar raises NoMatch, wreath returns None, so the caller's error branch has to move"),
    "orm.query.create": ("orm_query", "queries", NEEDS_REVIEW, "create(**values) -> instance = Model(**values); session.add(instance); await session.flush(). The rewrite is mechanical but the transaction boundary is not: ormar writes immediately, wreath writes when the session flushes, so where the flush goes is yours to choose"),
    "orm.query.all": ("orm_query", "queries", TRANSLATED, "all() -> await session.fetch(Model.select())"),
    "orm.query.eager": ("orm_query", "queries", NEEDS_REVIEW, "select_related/select_all/prefetch_related -> Model.select().include(Model.rel.selectin()). Wreath never lazy-loads, so a relationship you forget to include raises instead of silently N+1-ing; NPlusOneGuard catches the ones that slip through a handler. This call does not name its relations as plain literals — `select_all()` means *every* relation and wreath has no such switch, so write out the ones this caller actually reads"),
    "orm.query.eager_exact": ("orm_query", "queries", TRANSLATED, "select_related('rel')/prefetch_related('rel') -> Model.select().include(Model.rel.selectin()), one include per name, run with session.fetch(). Wreath never lazy-loads, so the include is mandatory rather than an optimisation — a relationship you forget raises instead of silently N+1-ing, and NPlusOneGuard catches the ones that slip through a handler"),
    "orm.query.values": ("orm_query", "queries", NEEDS_REVIEW, "values([...]) -> narrow the projection with Model.select(Model.a, Model.b); rows come back as models, not dicts"),
    "orm.query.bulk": ("orm_query", "queries", NEEDS_REVIEW, "bulk_create/bulk_update -> session.add() each instance and flush once; the flush batches by model"),
    "orm.query.count": ("orm_query", "queries", TRANSLATED, "count() -> await session.count(Model.select().where(...))"),
    "orm.query.exists": ("orm_query", "queries", TRANSLATED, "exists() -> await session.count(Model.select().where(...)) > 0 — wreath has no separate exists(); the count is the same round trip"),
    "orm.query.delete": ("orm_query", "queries", NEEDS_REVIEW, "delete() -> session.delete(instance) + flush for a loaded row; a bulk delete has no query form — issue it through postgres"),
    "orm.query.first": ("orm_query", "queries", NEEDS_REVIEW, "first() -> await session.fetch_one(Model.select().order_by(...).limit(1)); add the order_by, since 'first' without one is not deterministic"),
    "orm.query.get_or_create": ("orm_query", "queries", UNSUPPORTED, "get_or_create/update_or_create is a read-then-write race in one call — no wreath equivalent by design; write the upsert explicitly (ON CONFLICT) or guard it with a unique index"),
    "orm.query.order": ("orm_query", "queries", NEEDS_REVIEW, "order_by(...) -> Model.select().order_by(Model.col) / .desc(); this chain's columns are not literal strings, so the column each name resolves to is a lookup only you can do"),
    "orm.query.order_exact": ("orm_query", "queries", TRANSLATED, "order_by('col')/order_by('-col') -> Model.select().order_by(Model.col) / Model.col.desc(), run with session.fetch(). A trailing first() becomes session.fetch_one(...limit(1)) — the usual objection to first() is that an unordered 'first' is not deterministic, and this chain states the order, so there is nothing left to decide"),
    # -- middleware / lifespan / infra (not floor-checked) --------------------
    "mw.cors": ("middleware", "other", TRANSLATED, "add_middleware(CORSMiddleware, ...) -> add_middleware(CORSMiddleware(...)) (instance form)"),
    "mw.trustedhost": ("middleware", "other", TRANSLATED, "TrustedHostMiddleware -> wreath security middleware (instance form)"),
    "mw.custom": ("middleware", "other", NEEDS_REVIEW, "custom BaseHTTPMiddleware -> wreath's fused middleware base (rework); check built-ins first"),
    # The split at `yield` is determined only when it really is a split: a bare
    # `yield` at the top of the body partitions the statements in two, and each
    # half becomes a hook. It stops being a partition when a name made before
    # the yield is used after it (the halves are separate functions, so that name
    # needs a home), when the yield hands a value to the framework, or when it
    # sits inside a `try`/`async with` whose exit is the shutdown.
    "lifespan.ctx": ("lifespan", "other", NEEDS_REVIEW, "@asynccontextmanager lifespan -> @app.on_startup / @app.on_shutdown, but this body does not simply split at the yield"),
    "lifespan.split": ("lifespan", "other", TRANSLATED, "@asynccontextmanager lifespan with a bare top-level yield -> the statements before it become an `@app.on_startup` handler and the statements after it an `@app.on_shutdown` handler, in order; each takes the app. Nothing crosses the yield, so the split is a partition of the body"),
    # Now portable to a SHIPPED wreath subsystem (was unsupported): reviewable, not
    # auto-translatable (the task/loop body is bespoke) — needs-review with a real target.
    "bg.celery": ("background", "other", NEEDS_REVIEW, "Celery task -> wreath jobs: app.jobs()/@jobs.task + jobs.schedule(cron=) (built); port the task body by hand"),
    "bg.asyncio_loop": ("background", "other", NEEDS_REVIEW, "asyncio background loop -> a supervised wreath service or app.jobs() (built)"),
    "bg.multiprocessing": ("background", "other", NEEDS_REVIEW, "multiprocessing.Process worker -> jobs.launch() + ProgressRegistry (both built): the job runner owns the worker, and progress reports replace the shared state file a client polls -- jobs.launch() returns a TaskHandle whose task_id *is* the job id, so the status endpoint and the SSE stream need no second identifier. Port the worker body by hand"),
    # `wreath.graphql` shipped after this catalog was first written; leaving the
    # old "no equivalent" verdict in place told porters to keep a dependency
    # they can now delete, which is the specific way a porting tool goes stale.
    "graphql.mount": ("graphql", "other", NEEDS_REVIEW, "strawberry/GraphQL server -> wreath.graphql GraphQL(registry, models=[...]) mounted with .router(); the schema derives from the ORM registry rather than from declared types"),
    # A strawberry type that mirrors a model is a *deletion* — wreath derives the
    # object type from the ORM registry. But "mirrors a model" has to be proved,
    # not assumed, and two things break it. A type that lists a subset of the
    # columns is a deliberately narrowed surface, and the derived type exposes
    # every column of the model, so deleting the class WIDENS the public schema.
    # And strawberry camel-cases field names by default while wreath emits the
    # column name verbatim (`_graphql/schema.py` uses `column.python_name`), so a
    # snake_case field is a wire rename every client would see.
    "graphql.type": ("graphql", "other", NEEDS_REVIEW, "@strawberry.type/@strawberry.input -> wreath.graphql derives the type from the ORM model, so the class is usually deleted; `strawberry.auto` fields have no counterpart to write. Expose the model via GraphQL(models=[...]) — exposure is opt-in"),
    "graphql.type_mirror": ("graphql", "other", TRANSLATED, "@strawberry.type whose `strawberry.auto` fields are exactly the columns of the ORM model of the same name -> delete the class and name the model in GraphQL(models=[...]); the derived type is field-for-field the same, with the same names on the wire"),
    "graphql.resolver": ("graphql", "other", NEEDS_REVIEW, "@strawberry.field computed field -> @api.field(\"Type\", \"name\", returns=...); the resolver sees the whole level (batched), not one object"),
    # boto3 is not one verdict. Object storage became a framework feature when
    # `wreath.objects` shipped (design 09), so an S3 client now has a real target
    # and reporting it as "keep the external library" tells a porter to keep a
    # dependency they can delete. Every other AWS service still has none, so the
    # service name is what splits them — read it rather than judging the import.
    "ext.boto3": ("external", "other", UNSUPPORTED, "boto3/AWS SDK is not a framework feature; keep the external library"),
    "ext.boto3_s3": ("external", "other", NEEDS_REVIEW, "boto3 S3 client/resource -> wreath.objects ObjectStore: S3ObjectStore(bucket=, region=) with ObjectPath for keys, put/get/stat/delete and zip_stream (built, design 09). Signing is SigV4 either way; what changes is that the store is declared once on the app and drained by lifespan rather than constructed at import. A presigned-URL flow has a recipe"),
    "webhook.hmac": ("webhook", "other", NEEDS_REVIEW, "hand-rolled HMAC webhook signature verify -> wreath.webhooks HMACWebhookVerifier.verify(), which checks the digest with compare_digest *and* the timestamp against a replay window, and refuses an envelope whose relay path it has already seen. The hand-rolled form here compares the digest only, so a captured request replays forever — port the secret and the header names, not the comparison (built)"),
    "ext.aiometer": ("external", "other", NEEDS_REVIEW, "aiometer/tenacity outbound throttle+retry -> app.http_client(rate=, retries=) (built)"),
    "ext.s3path": ("external", "other", NEEDS_REVIEW, "s3path.S3Path -> wreath.objects ObjectStore/ObjectPath (built, design 09)"),
    "ext.gql": ("external", "other", UNSUPPORTED, "gql GraphQL client has no wreath equivalent; keep the external library"),
    "form.as_form": ("form_binding", "other", TRANSLATED, "as_form decorator deleted; consuming `Depends(Model.as_form)` -> `Annotated[Model, Form()]` whole-model multipart binding (built)"),
    "lock.dlock": ("advisory_lock", "other", NEEDS_REVIEW, "sqlalchemy-dlock -> wreath advisory locks db.lock()/db.try_lock()/Session.lock() (built, design 03)"),
    "auth.jwt": ("auth", "other", NEEDS_REVIEW, "manual JWT/JWKS verify -> app.oidc_provider()/BearerTokenBackend/JwtVerifier (built, design 02)"),
    "auth.oauth": ("auth", "other", NEEDS_REVIEW, "authlib OAuth2/client-credentials -> wreath oauth2_login()/ClientCredentials (built, design 02)"),
    "mig.manual": ("migration_op", "other", UNSUPPORTED, "a `postgresql_using=` cast (or index method) is a MANUAL op — the generator cannot derive it from a model; keep Alembic (design 07 Alembic posture)"),
    # Alembic operations are the single biggest file count in a mature app (~1400
    # in the corpus). Most are ordinary DDL that `wreath migrations generate`
    # derives from the models; the ones that are not are worth separating,
    # because they are the ones that make a deploy slow, risky, or wrong.
    #
    # `mig.derived` is translated for the same reason `pydantic.config_forbid`
    # and `resp.jsonable` are: the determined target is *no hand-written code*.
    # Wreath's migration source of truth is the ORM image, and detection covers
    # tables, columns (type, nullability, identity, generated, server default),
    # primary keys, unique constraints, foreign keys and btree indexes
    # (docs/from-fastapi/alembic.md, "What `detect` sees"). Every operation in
    # that set is a function of the model change the porter is already making,
    # so there is nothing left to decide at the revision. What is NOT in that
    # set gets its own verdict below rather than riding along on this one.
    "mig.derived": ("migration_op", "other", TRANSLATED, "ordinary DDL over objects wreath models -> nothing to hand-write: `wreath migrations detect` reads this off the model change and `generate` emits the artifact. Confirm the ported model declares the end state (wreath is NOT NULL by default), then let the generator own the revision; a drop needs --allow-destructive when it is applied"),
    "mig.schema_op": ("migration_op", "other", NEEDS_REVIEW, "a schema operation over an object wreath's ORM does not model yet (a check/exclusion constraint, an unnamed constraint kind, a non-literal argument) — the generator has no model attribute to derive it from, so decide whether the object moves onto the model or stays in Alembic"),
    "mig.rename": ("migration_op", "other", NEEDS_REVIEW, "a RENAME is the one ordinary-looking op a model differ gets wrong: `detect` compares images, so a renamed table or column reads as one object dropped and another created — which would move no data. Keep this revision in Alembic, or rename in the database first and let detect see a matching image"),
    "mig.index_manual": ("migration_op", "other", UNSUPPORTED, "an expression, partial, covering or non-btree index is emitted as a MANUAL operation that cannot be applied (and therefore cannot be downgraded) — keep it in Alembic; wreath's detection covers btree indexes only today"),
    "mig.unmodelled_type": ("migration_op", "other", NEEDS_REVIEW, "this DDL names a column type wreath's ORM has no PgType for (Numeric/Decimal, Time, Interval, Enum, INET, TSVECTOR, ...), so the generator cannot derive the column; pick a modelled type or keep the table in Alembic"),
    "mig.raw_sql": ("migration_op", "other", UNSUPPORTED, "op.execute(<raw SQL>) is a MANUAL op — the generator cannot derive it from a model; keep it in Alembic (design 07 Alembic posture)"),
    # Deferred data migrations shipped (design 24), so "keep it in Alembic" stopped
    # being true. The verdict stays needs-review because the *body* is bespoke —
    # a Recode wants the old->new mapping written out, which is the thing the
    # `op.execute(UPDATE ...)` in this revision encodes and a differ cannot read.
    "mig.data": ("migration_op", "other", NEEDS_REVIEW, "op.get_bind() means this revision rewrites *rows*, not just schema — the migration that blocks a deploy for an hour on a large table. Wreath now ships deferred data migrations: declare a `Recode(Model.col, mapping={...})` beside the model (same column, new values) or a `Retype` (new column, backfill, verify, swap) and drive it with jobs.drive(). Startup applies the DDL and serves immediately while a chunked pass converts rows, and `wreath migrations check` refuses a later migration that narrows the column before the pass has published. The mapping is yours to write — that is what makes this needs-review rather than automatic"),
    # -- caching --------------------------------------------------------------
    "cache.store": ("cache", "other", TRANSLATED, "cachetools TTLCache(maxsize=, ttl=)/LRUCache(maxsize=) -> wreath.cache.BoundedCache(max_entries=, ttl=) — the same bounded LRU with the same eviction, under the framework's own budget. (A read-mostly reference table is better served by SnapshotCache + refresh_on, but that is a change of shape, not a rename.)"),
    "cache.decorator": ("cache", "other", NEEDS_REVIEW, "@cachetools.cached -> @wreath.response_cache.cached(ttl=, invalidate_on=[Model]). A TTL is a guess; naming the models makes it exact — the ORM announces its committed writes and the cache clears. Add cache.invalidate_across_workers(bus) to make that fleet-wide, and cache.refresh_on() for a SnapshotCache (built)"),
    # -- time -----------------------------------------------------------------
    #
    # `wreath.temporal` shipped, so arrow stops being a dependency you have to
    # replace with hand-rolled stdlib and becomes a rename. The catalog said "do
    # not wait for it" while it was designed-not-shipped; leaving that in place
    # once it landed would tell porters to write the code wreath now owns.
    "time.arrow": ("time", "other", TRANSLATED, "arrow -> wreath.temporal, a rename per call: arrow.utcnow()/arrow.now() -> temporal.now(); arrow.get(s) -> temporal.parse(s); .humanize() -> temporal.relative(value). An Instant is a datetime subclass, so it stores, compares, and serializes without a conversion at the edges — and it refuses to be naive, which is the bug arrow's implicit UTC hides"),
    "time.arrow_other": ("time", "other", NEEDS_REVIEW, "an arrow construct with no straight rename (Arrow(...), .range()/.interval(), a `.shift(months=)`) -> temporal covers the clock, parsing, and relative formatting; a calendar shift by months or years is not a fixed number of seconds and temporal will not pretend otherwise, so pick the behaviour you meant"),
    # -- responses ------------------------------------------------------------
    "resp.class": ("response", "other", TRANSLATED, "fastapi.responses.<X> -> wreath.response.<X> (JSON/HTML/Redirect/PlainText/Streaming/File all exist)"),
    "resp.status_const": ("response", "other", TRANSLATED, "fastapi.status.HTTP_* -> the plain int, or the matching wreath exception class when it is raised"),
    "resp.jsonable": ("response", "other", TRANSLATED, "jsonable_encoder(x) -> drop it; wreath's JSON codec serializes dataclasses, ORM rows, UUIDs, and datetimes directly"),
    "route.response_class": ("route_option", "other", NEEDS_REVIEW, "response_class= -> return the wreath response type from the handler instead of declaring it on the route"),
    # -- auth schemes ---------------------------------------------------------
    "auth.security_scheme": ("auth", "other", NEEDS_REVIEW, "declarative security scheme (HTTPBearer/HTTPBasic/APIKeyHeader/OAuth2PasswordBearer) -> configure_auth(BearerTokenBackend(...)/ApiKeyBackend(...)); wreath authenticates once at the route boundary rather than per dependency (built, design 02)"),
    "auth.security": ("auth", "other", NEEDS_REVIEW, "Security(scheme, scopes=[...]) -> Depends() plus @permissions(...)/@roles(...); wreath has no scope slot on the dependency itself"),
    # -- the test suite -------------------------------------------------------
    "test.client": ("test", "other", NEEDS_REVIEW, "fastapi.testclient.TestClient -> wreath.testing.TestClient, which is **async**: `async with TestClient(app) as client:` and `await client.get(...)`; responses expose .status (not .status_code)"),
    "test.dependency_override": ("test", "other", NEEDS_REVIEW, "app.dependency_overrides[dep] = ... has no wreath equivalent. For the common case (swapping the auth dependency) use TestClient.acting_as(\"rider\", roles=[...]) instead; for a swapped repository/session, inject it through app.state or a factory the test controls (built)"),
    # -- libraries that are not framework features ----------------------------
    "ext.pandas": ("external", "other", UNSUPPORTED, "pandas/numpy analysis code is not a framework feature; keep the library and the module as-is"),
    "ext.httpx": ("external", "other", NEEDS_REVIEW, "httpx.AsyncClient -> app.http_client(base_url=, rate=, retries=), a managed pool with native codecs started and drained by lifespan (built)"),
    # -- confidence -----------------------------------------------------------
    "resolve.star_import": ("star_import", "other", NEEDS_REVIEW, "`from x import *` reduces name-resolution confidence for this module"),
}


def rule(rule_id: str) -> tuple[str, str, str, str]:
    return RULES[rule_id]
