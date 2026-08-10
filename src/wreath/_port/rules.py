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
    "route.app": ("app", "routing", TRANSLATED, "FastAPI() becomes Wreath()."),
    "route.router": (
        "router",
        "routing",
        TRANSLATED,
        "APIRouter(prefix=..., tags=...) becomes Router(prefix=..., tags=...).",
    ),
    "route.method": (
        "route",
        "routing",
        TRANSLATED,
        "The route decorator is unchanged. The handler takes request: Request as its first parameter.",
    ),
    "route.include_static": (
        "include_router",
        "routing",
        TRANSLATED,
        "include_router() is unchanged.",
    ),
    "route.include_dynamic": (
        "include_router",
        "routing",
        TRANSLATED,
        "A loop around include_router() is unchanged: each Router is still included when the loop executes.",
    ),
    "route.websocket": (
        "route",
        "routing",
        TRANSLATED,
        "The websocket decorator is unchanged on Wreath and Router. Import WebSocket and WebSocketDisconnect from wreath.websocket.",
    ),
    "ws.json_method": (
        "websocket",
        "other",
        TRANSLATED,
        "send_json() and receive_json() are unchanged and use Wreath's JSON codec.",
    ),
    # -- route options (not floor-checked; counted in overall) ----------------
    "route.response_model": (
        "route_option",
        "other",
        TRANSLATED,
        "Drop response_model= and put the public model on the handler's return annotation. Wreath filters and validates plain return values through that annotation at runtime; an explicit Response keeps ownership of its wire body.",
    ),
    # `status_code=` is a route slot for coerced values. An explicitly returned
    # Response still owns its own status, so those shapes remain distinct.
    "route.status_code": (
        "route_option",
        "other",
        NEEDS_REVIEW,
        "Keep status_code= on the route. It applies to plain coerced values and the OpenAPI success response; an explicit Response still owns its own status, so confirm which return shape this handler uses.",
    ),
    "route.status_code_return": (
        "route_option",
        "other",
        TRANSLATED,
        "Return JSONResponse(<value>, status=<number>) and drop status_code= from the route. wreath already sends a dict, list or number as JSON, so only the status changes.",
    ),
    "route.status_code_text": (
        "route_option",
        "other",
        TRANSLATED,
        "Return TextResponse(<value>, status=<number>) and drop status_code= from the route. A string is text/plain in wreath, so JSONResponse would change the content type as well as the status.",
    ),
    "route.status_code_response": (
        "route_option",
        "other",
        TRANSLATED,
        "Drop status_code= from the route and pass status= to the response this handler already returns. The route-level value was doing nothing: the returned response's own status wins.",
    ),
    "route.status_code_empty": (
        "route_option",
        "other",
        TRANSLATED,
        "Return Response(status=<number>). A handler that returns nothing would otherwise answer 200 with a JSON null, and Response leaves out content-length for a status that carries no body.",
    ),
    "route.status_code_empty_body": (
        "route_option",
        "other",
        NEEDS_REVIEW,
        "This route says 204 or 304, which must have no body, but the handler returns one. FastAPI let that through. Decide which is right before porting: drop the return, or use a status that allows a body.",
    ),
    "route.include_in_schema": (
        "route_option",
        "other",
        TRANSLATED,
        "include_in_schema= is unchanged; false withholds the route from OpenAPI and generated clients.",
    ),
    # -- params ---------------------------------------------------------------
    "param.query": (
        "param",
        "params",
        TRANSLATED,
        "Query(default, ge=, le=) -> Annotated[T, Query(minimum=, maximum=)] = default",
    ),
    "param.query_strconstraint": (
        "param",
        "params",
        NEEDS_REVIEW,
        "Wreath's Query marker carries a minimum and a maximum for numbers and nothing else, so a length or pattern rule on a query parameter has no home. Either check it in the handler and raise UnprocessableEntity, or move the value into a request body where a model can validate it.",
    ),
    "param.path": ("param", "params", TRANSLATED, "Path(...) -> Annotated[T, Path()]"),
    "param.header": ("param", "params", TRANSLATED, "Header(...) -> Annotated[T, Header(alias=)]"),
    "param.cookie": ("param", "params", TRANSLATED, "Cookie(...) -> Annotated[T, Cookie()]"),
    "param.form": ("param", "params", TRANSLATED, "Form(...) -> Annotated[T, Form()]"),
    "param.file": (
        "param",
        "params",
        TRANSLATED,
        "File()/UploadFile -> Annotated[UploadFile, File()]",
    ),
    "param.body": (
        "param",
        "params",
        TRANSLATED,
        "A parameter annotated with a model is the request body, with no marker needed. The model becomes a dataclass.",
    ),
    "param.body_embed": (
        "param",
        "params",
        NEEDS_REVIEW,
        "embed=True wraps the body in a single key named after the parameter, and wreath has no switch for it. Either add that wrapping field to the model, or drop embed and send the object unwrapped.",
    ),
    # -- pydantic models ------------------------------------------------------
    "pydantic.model": ("model", "pydantic_models", TRANSLATED, "class X(BaseModel) -> @dataclass"),
    # A dataclass has one slot per field -- the default -- so a `Field(...)`
    # translates when everything it carries is a default. Documentation and
    # constraints now have a runtime home in Annotated[wreath.binding.Field].
    "pydantic.field": (
        "field",
        "pydantic_models",
        TRANSLATED,
        "plain field maps 1:1: `Field(default=x)`/`Field(x)` -> `= x`, `Field(default_factory=f)` -> `= field(default_factory=f)`, and a list/dict/set default -> field(default_factory=...). Keep descriptions, examples, aliases and constraints in Annotated[T, wreath.binding.Field(...)].",
    ),
    # Pydantic does not care what order defaulted and required fields are
    # declared in. `@dataclass` does, and raises at class-creation time -- which
    # `ast.parse` and `compile` both accept, so this port used to fail only when
    # the module was first imported, and it is an ordinary shape to write.
    "pydantic.model_kw_only": (
        "model",
        "pydantic_models",
        NEEDS_REVIEW,
        "A field with no default is declared after one that has a default. Pydantic does not mind; a dataclass refuses to be built at all. This is now @dataclass(kw_only=True), which fixes it and is how wreath builds request bodies anyway. The one thing to check: anything that constructs this model with positional arguments has to switch to keywords.",
    ),
    "pydantic.model_kw_only_exact": (
        "model",
        "pydantic_models",
        TRANSLATED,
        "This becomes @dataclass(kw_only=True). No call in the analyzed tree constructs the model positionally, so making its existing keyword-only behavior explicit changes no call site.",
    ),
    "pydantic.field_marker": (
        "field",
        "pydantic_models",
        NEEDS_REVIEW,
        "Move alias, description and examples to Annotated[T, wreath.binding.Field(...)]. discriminator=, exclude= and strict= still need a design decision.",
    ),
    "pydantic.field_constraint": (
        "field",
        "pydantic_models",
        NEEDS_REVIEW,
        "Move gt/ge/lt/le, min_length/max_length and pattern to Annotated[T, wreath.binding.Field(...)]. Keep a matching ORM check as well when the value is persisted.",
    ),
    # -- pydantic extras (not floor-checked) ----------------------------------
    "pydantic.config_forbid": (
        "config",
        "other",
        TRANSLATED,
        "Drop extra='forbid'. Rejecting unknown fields is already what wreath does.",
    ),
    "pydantic.config_ignore": (
        "config",
        "other",
        NEEDS_REVIEW,
        "extra='ignore' means unknown fields were dropped quietly. Wreath always rejects them with a 422 and cannot be told otherwise, so any client sending extra keys will start getting errors. Either stop sending them or add the fields to the model.",
    ),
    "pydantic.config_class": (
        "config",
        "other",
        NEEDS_REVIEW,
        "This is pydantic v1's nested Config class. Delete it. Rejecting unknown fields is already wreath's behaviour; anything else it set has to be moved by hand.",
    ),
    "pydantic.validator": (
        "validator",
        "other",
        NEEDS_REVIEW,
        "A validator is code, so it has to be moved by hand. For a rule about one field, use narrow() on the column; for a rule spanning fields, use @rule(). Both run once when the app starts rather than on every request.",
    ),
    "pydantic.get_pydantic": (
        "get_pydantic",
        "other",
        UNSUPPORTED,
        "This get_pydantic() shape is dynamic or feeds another model transformer, so its resulting fields cannot be proved statically. Write out the dataclass or replace the dynamic step with model_dataclass().",
    ),
    "pydantic.get_pydantic_exact": (
        "get_pydantic",
        "other",
        TRANSLATED,
        "A literal include=/exclude= projection becomes model_dataclass(Model, ..., name=...). It compiles an ordinary keyword-only dataclass once at declaration time for binding, OpenAPI, and type generation.",
    ),
    # -- dependencies ---------------------------------------------------------
    "depends.use": (
        "depends",
        "dependencies",
        TRANSLATED,
        "Depends(...) is unchanged. The function it points at takes request as its first parameter, like a handler.",
    ),
    "depends.router_call": (
        "depends_wiring",
        "other",
        NEEDS_REVIEW,
        "The router's dependencies= is a call rather than a plain list, so this tool cannot see what is in it. Write the Depends(...) entries out as a list.",
    ),
    # -- ORM models -----------------------------------------------------------
    "orm.model": (
        "orm_model",
        "orm_models",
        TRANSLATED,
        'This model becomes wreath.orm.Model with table="<name>" on the class header, and each field an annotated column().',
    ),
    "orm.column": (
        "column",
        "orm_models",
        TRANSLATED,
        "Column types map onto wreath.orm.types. The one thing to check is emptiness: ormar allowed a column to be empty unless told otherwise, wreath requires a value unless told otherwise.",
    ),
    "orm.fk": (
        "column",
        "orm_models",
        NEEDS_REVIEW,
        "This foreign key points at a model this tool could not find, so the column type is a guess (Uuid). Open the model it references and set the column to the same type as its primary key.",
    ),
    "orm.fk_typed": (
        "column",
        "orm_models",
        TRANSLATED,
        'The foreign key becomes two lines: a column() holding the id, typed to match the primary key it points at, and a relationship() for the object. load="raise" means wreath will not fetch it behind your back -- include it in the query when you need it.',
    ),
    # -- exceptions -----------------------------------------------------------
    "exc.http_literal": (
        "httpexception",
        "exceptions",
        TRANSLATED,
        "HTTPException(status_code=<int>) -> the matching wreath exception class, with the detail as its first positional argument. A 500 becomes `HTTPException(detail)` itself: wreath's base class declares `status = 500`",
    ),
    "exc.http_variable": (
        "httpexception",
        "exceptions",
        NEEDS_REVIEW,
        "The status here is computed, so the right wreath exception cannot be chosen for you. If the value has a small set of possibilities, raise the matching class (NotFound, Forbidden, Conflict, ...); otherwise raise HTTPException(detail) from wreath.exceptions and set status on a subclass.",
    ),
    "exc.http_unmapped": (
        "httpexception",
        "exceptions",
        NEEDS_REVIEW,
        "Two things can land here. Either wreath ships no exception class for this status -- subclass HTTPException and set status = <the number> -- or the call passes headers=, which wreath takes as a list of lowercase byte pairs ([(b'retry-after', b'30')]) rather than a dict of strings. Both matter: a 401 without its challenge header and a 429 without Retry-After are broken responses, so nothing was dropped for you.",
    ),
    "exc.handler": (
        "exception_handler",
        "exceptions",
        TRANSLATED,
        "@app.exception_handler(...) is unchanged.",
    ),
    # -- settings -------------------------------------------------------------
    #
    # Split by *field shape*, the same way `.objects.filter()` is split by
    # argument shape. A `BaseSettings` class of plain scalars with literal
    # defaults is a mechanical rewrite: pydantic-settings' default source reads
    # the field name (case-insensitively, so upper-case is the canonical
    # spelling) with `env_prefix` in front, no default means required, and
    # Environment.bind owns conversion, nested groups, aggregate errors and
    # defaults. Validators and JSON-valued settings still require judgment.
    "settings.class": (
        "settings",
        "settings",
        NEEDS_REVIEW,
        "Make this an ordinary dataclass and construct it with Environment.load('.env').bind(Settings, prefix=...). Field validators and JSON-valued settings still need an explicit conversion decision.",
    ),
    "settings.class_env": (
        "settings",
        "settings",
        TRANSLATED,
        "Make this an ordinary dataclass and bind it with Environment.load('.env').bind(Settings, prefix=...). Required fields, literal defaults and scalar conversion are automatic.",
    ),
    "settings.field": (
        "settings",
        "settings",
        TRANSLATED,
        "Environment.bind converts the annotated scalar and uses the dataclass default when the key is absent.",
    ),
    "settings.field_complex": (
        "settings",
        "settings",
        NEEDS_REVIEW,
        "Environment.bind supports optionals, unions and comma-separated containers. A JSON object encoded into one variable or a custom validator still needs an explicit adapter.",
    ),
    "settings.nested": (
        "settings",
        "settings",
        NEEDS_REVIEW,
        "Decide whether to keep the nested dataclass or flatten its access path. Environment.bind reads a kept group's fields with a double underscore, such as APP_DATABASE__HOST; a pydantic-settings JSON value for the whole group still needs an explicit adapter.",
    ),
    # -- queries ---------------------------------------------------------------
    #
    # `.objects.` is the largest single construct in a real ormar codebase — of
    # the order of a third of every framework token in one.
    # One generic verdict for all of it reports the *size* of the job
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
    "orm.query": (
        "orm_query",
        "queries",
        UNSUPPORTED,
        "This is an ormar query and it was left as written. Queries become Model.select() with .where(...) on it, run through a session: await session.fetch(...) for a list, fetch_one(...) for one row, count(...) for a number.",
    ),
    # The emitter writes the determined queries out in full, and can only do that
    # where a session is in scope. Inside a route handler wreath supplies one;
    # anywhere else the function has to take one, and that is a change to every
    # caller — so it is one note on the function rather than one per query.
    # What `--opinionated` does instead of leaving `orm.query.needs_session`.
    # Still needs-review, and for a reason that has nothing to do with the query:
    # the signature changed, so the callers have to catch up.
    "orm.query.session_added": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "this function now takes a session, because it runs queries or calls something that does. Every call to it inside the ported tree was updated to pass one; anything calling it from outside has to be updated by hand.",
    ),
    "orm.query.needs_session": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "Queries in this function were left alone because wreath runs them through a session and there is none here. Add a session: Session parameter to this function and pass one in from each caller -- a route handler gets one for free by declaring session: Annotated[Session, FromORM()]. Once it is in scope, each query below becomes the Model.select() form its own note describes.",
    ),
    "orm.query.filter": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This filter was left as written because one of its lookups needs a decision. A lookup across a relation (owner__name) becomes Model.owner.name -- wreath adds the join itself, but it has to be told which model owner points at, and that model is usually in another file. A JSON lookup needs you to pick the containment operator. Everything else about the query is mechanical: Model.select().where(...), run with session.fetch().",
    ),
    "orm.query.filter_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "Every lookup here carries straight across: filter(...) becomes Model.select().where(Model.col == value), with __gte as >= and __in as .in_(...). Run it with await session.fetch(...) for a list or session.count(...) for a number. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.get_or_none": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "Same as the filter note: one of the lookups here does not carry across on its own. The call itself becomes await session.fetch_one(Model.select().where(...)), which returns None on no match exactly as get_or_none did.",
    ),
    "orm.query.get_or_none_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.fetch_one(Model.select().where(...)). It behaves the same as get_or_none: None when nothing matches, an error when more than one row does. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.get": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This get() has a dynamic predicate or positional query object, so the required-row rewrite cannot be proved. Static keyword lookups become session.require(...) or session.require_one(...), both of which preserve the exception-on-miss contract.",
    ),
    "orm.query.get_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "get(id=value) becomes await session.require(Model, value); other static lookups become await session.require_one(Model.select().where(...)). Both preserve the exception-on-miss and multiple-row contracts. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.create": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This create() uses positional input, so it cannot become a keyword-only Wreath model construction without a decision.",
    ),
    "orm.query.create_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.create(Model, **values) preserves immediate insertion while keeping construction and validation on the native Wreath model. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.all": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.fetch(Model.select()). Pass --opinionated and this is written out for you.",
    ),
    "orm.query.page_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "limit(n)/offset(n) becomes Model.select().limit(n)/offset(n), run with session.fetch(...). Pass --opinionated and this is written out for you.",
    ),
    "orm.query.eager": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This call does not name the relations to load as plain strings -- select_all() means every relation, and wreath has no such switch. Write out the ones this code actually reads, one .include(Model.rel.selectin()) each. It matters more than it did: wreath never loads a relation behind your back, so one you forget raises instead of quietly running an extra query per row.",
    ),
    "orm.query.select_all": (
        "orm_query",
        "queries",
        UNSUPPORTED,
        "select_all() is deliberately not portable. Unbounded graph expansion hides query count and response size. Name the few relationships the use case reads, or write one explicit SQL projection/JSON aggregate for a genuinely wide response; Wreath will not recreate the switch.",
    ),
    "orm.query.eager_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "One .include(Model.rel.selectin()) per relation named here, on a Model.select() run with session.fetch(). The include is not optional in wreath the way select_related was an optimisation: a relation you do not include raises when touched, instead of quietly running an extra query per row. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.values": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "values([...]) returned dictionaries. The wreath equivalent, Model.select(Model.a, Model.b), returns model objects with only those columns filled in -- so the code reading these rows has to use attributes instead of keys.",
    ),
    "orm.query.bulk": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "bulk_create/bulk_update becomes session.add() for each row followed by a single await session.flush(). The flush batches the inserts by model, so this is still one round trip per model rather than one per row.",
    ),
    "orm.query.count": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.count(Model.select().where(...)). Pass --opinionated and this is written out for you.",
    ),
    "orm.query.exists": (
        "orm_query",
        "queries",
        TRANSLATED,
        "wreath has no exists(); count the rows instead -- await session.count(Model.select().where(...)) > 0. It is the same single round trip. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.delete": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "For a row already loaded, session.delete(row) then await session.flush(). A statically filtered bulk delete becomes session.delete_where(Model.select().where(...)); predicate-free bulk writes are refused.",
    ),
    "orm.query.first": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "first() becomes await session.fetch_one(Model.select().order_by(...).limit(1)) -- and you have to supply the order_by. Without one, 'the first row' is whatever the database happens to return, which is why wreath makes you say it.",
    ),
    "orm.query.get_or_create": (
        "orm_query",
        "queries",
        UNSUPPORTED,
        "get_or_create looks up a row and creates it if it is missing, in one call. Two requests can run that at the same time and both create. Wreath has no equivalent on purpose: write the insert with ON CONFLICT, or add a unique index and catch the violation.",
    ),
    "orm.query.order": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "The columns to order by are not plain strings here, so this tool cannot tell which columns they are. Written out, order_by('name') is .order_by(Model.name) and order_by('-created') is .order_by(Model.created.desc()).",
    ),
    "orm.query.order_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "order_by('name') becomes .order_by(Model.name) and order_by('-created') becomes .order_by(Model.created.desc()), on a Model.select() run with session.fetch(). Pass --opinionated and this is written out for you.",
    ),
    # -- middleware / lifespan / infra (not floor-checked) --------------------
    "mw.cors": (
        "middleware",
        "other",
        TRANSLATED,
        "add_middleware(CORSMiddleware, ...) -> configure_http_policy(HttpPolicy(cors=CorsPolicy(...)))",
    ),
    "mw.trustedhost": (
        "middleware",
        "other",
        TRANSLATED,
        "TrustedHostMiddleware -> first-class TrustedHostPolicy",
    ),
    "mw.custom": (
        "middleware",
        "other",
        NEEDS_REVIEW,
        "This is a custom BaseHTTPMiddleware. Check wreath's built-in middleware first, since much of what apps write by hand is already there. If it is genuinely yours, rework it onto wreath's middleware base -- the shape is different: wreath fuses the whole chain at startup instead of nesting one call per layer.",
    ),
    # The split at `yield` is determined only when it really is a split: a bare
    # `yield` at the top of the body partitions the statements in two, and each
    # half becomes a hook. It stops being a partition when a name made before
    # the yield is used after it (the halves are separate functions, so that name
    # needs a home), when the yield hands a value to the framework, or when it
    # sits inside a `try`/`async with` whose exit is the shutdown.
    "lifespan.ctx": (
        "lifespan",
        "other",
        NEEDS_REVIEW,
        "Startup and shutdown become two functions, @app.on_startup and @app.on_shutdown. This body does not split cleanly at the yield, so the division is yours to make -- the note in brackets says what is in the way.",
    ),
    "lifespan.split": (
        "lifespan",
        "other",
        TRANSLATED,
        "This body splits cleanly at the yield: everything before it becomes an @app.on_startup function and everything after it an @app.on_shutdown one, in the same order. Each takes the app.",
    ),
    # Now portable to a SHIPPED wreath subsystem (was unsupported): reviewable, not
    # auto-translatable (the task/loop body is bespoke) — needs-review with a real target.
    "bg.celery": (
        "background",
        "other",
        NEEDS_REVIEW,
        "Celery has a replacement built in: app.jobs() with @jobs.task, and jobs.schedule(cron=...) for anything periodic. The wiring is a rename; the body of the task moves across as it is.",
    ),
    "bg.asyncio_loop": (
        "background",
        "other",
        NEEDS_REVIEW,
        "A loop started with asyncio.create_task has nothing supervising it -- if it raises, it stops and nothing says so. Move the work into app.jobs() or a supervised wreath service, which restarts it and reports failures.",
    ),
    "bg.asyncio_joined": (
        "background",
        "other",
        TRANSLATED,
        "This task is joined in the same async function that creates it, so it is structured request/test concurrency rather than a background service. Keep asyncio.create_task: its lifetime is already bounded and its exception is observed.",
    ),
    "bg.multiprocessing": (
        "background",
        "other",
        NEEDS_REVIEW,
        "Replace the worker process with jobs.launch(), and the shared file or table the client polls with progress reports. jobs.launch() hands back a task whose id is the job id, so the status endpoint and the progress stream need no second identifier. The body of the worker moves across as it is.",
    ),
    # `wreath.graphql` shipped after this catalog was first written; leaving the
    # old "no equivalent" verdict in place told porters to keep a dependency
    # they can now delete, which is the specific way a porting tool goes stale.
    "graphql.mount": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        "Wreath ships GraphQL: GraphQL(registry, models=[...]) mounted with .router(). The difference is where the schema comes from -- wreath builds it from the ORM models you name, instead of from types you declare.",
    ),
    # A strawberry type that mirrors a model is a *deletion* — wreath derives the
    # object type from the ORM registry. But "mirrors a model" has to be proved,
    # not assumed, and two things break it. A type that lists a subset of the
    # columns is a deliberately narrowed surface, and the derived type exposes
    # every column of the model, so deleting the class WIDENS the public schema.
    # And strawberry camel-cases field names by default while wreath emits the
    # column name verbatim (`_graphql/schema.py` uses `column.python_name`), so a
    # snake_case field is a wire rename every client would see.
    "graphql.type": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        "Wreath builds the GraphQL type from the ORM model, so this class usually just goes away; name the model in GraphQL(models=[...]) instead. It was not deleted for you because deleting it here would change the schema -- the note in brackets says how.",
    ),
    "graphql.type_dataclass": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        "This plain output type becomes a native @dataclass(kw_only=True) and is registered with GraphQL(dataclasses=[...]). The porter removes the Strawberry decorator; add the class to that explicit schema allowlist where the endpoint is assembled.",
    ),
    "graphql.type_mirror": (
        "graphql",
        "other",
        TRANSLATED,
        "This class lists exactly the columns of the model of the same name, so it can be deleted -- name the model in GraphQL(models=[...]) instead and wreath builds the same type, with the same field names on the wire.",
    ),
    "graphql.resolver": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        'A computed field becomes api.field("Type", "name", returns=...). One difference to plan for: your resolver is called once for the whole level with every parent object, not once per object, so it returns a list.',
    ),
    # boto3 is not one verdict. Object storage became a framework feature when
    # `wreath.objects` shipped (design 09), so an S3 client now has a real target
    # and reporting it as "keep the external library" tells a porter to keep a
    # dependency they can delete. Every other AWS service still has none, so the
    # service name is what splits them — read it rather than judging the import.
    "ext.boto3": (
        "external",
        "other",
        UNSUPPORTED,
        "This talks to an AWS service wreath has no equivalent for. Keep boto3 and this code as it is.",
    ),
    "ext.boto3_s3": (
        "external",
        "other",
        NEEDS_REVIEW,
        "S3 has a replacement built in: S3ObjectStore(bucket=..., region=...) from wreath.objects, with put, get, stat, delete and zip_stream, and ObjectPath for keys. Signing works the same way. What changes is the lifecycle -- the store is declared once on the app and closed for you, instead of being built at import time. There is a recipe for presigned URLs.",
    ),
    "webhook.hmac": (
        "webhook",
        "other",
        NEEDS_REVIEW,
        "This checks a webhook signature by hand, and it only compares the digest -- so anyone who captures a valid request can replay it forever. HMACWebhookVerifier.verify() from wreath.webhooks compares the digest safely, checks the timestamp against a replay window, and refuses an envelope it has already seen. Port the secret and the header names; do not port the comparison.",
    ),
    "ext.aiometer": (
        "external",
        "other",
        NEEDS_REVIEW,
        "Rate limiting and retries around outbound calls are built into the HTTP client: app.http_client(rate=..., retries=...). Drop aiometer and tenacity and set them there.",
    ),
    "ext.s3path": (
        "external",
        "other",
        NEEDS_REVIEW,
        "S3Path becomes ObjectPath with an ObjectStore from wreath.objects.",
    ),
    "ext.gql": (
        "external",
        "other",
        UNSUPPORTED,
        "This is a GraphQL *client*. Wreath serves GraphQL but does not consume it, so keep the gql library.",
    ),
    "form.as_form": (
        "form_binding",
        "other",
        TRANSLATED,
        "Delete the as_form decorator. A parameter written Annotated[Model, Form()] binds a whole multipart form to the model.",
    ),
    "lock.dlock": (
        "advisory_lock",
        "other",
        NEEDS_REVIEW,
        "Advisory locks are built in: db.lock() and db.try_lock() on the database, or session.lock() inside a transaction. Drop sqlalchemy-dlock.",
    ),
    "auth.jwt": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "Verifying a JWT by hand is easy to get subtly wrong. Wreath does it for you: app.oidc_provider() for a standard provider, or BearerTokenBackend with JwtVerifier for a token you issue. Both fetch and cache signing keys and check the claims.",
    ),
    "auth.oauth": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "OAuth is built in: oauth2_login() for a user sign-in flow, ClientCredentials for machine-to-machine. Drop authlib.",
    ),
    "mig.manual": (
        "migration_op",
        "other",
        UNSUPPORTED,
        "postgresql_using= is a cast that only you can write -- nothing about the model says how the old values become the new ones. Keep this revision in Alembic.",
    ),
    # Alembic operations are the single biggest file count in a mature app.
    # Most are ordinary DDL that `wreath migrations generate`
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
    "mig.derived": (
        "migration_op",
        "other",
        TRANSLATED,
        "There is nothing to write here. wreath compares the models with the database and produces this migration itself: check the ported model declares the end state, then run `wreath migrations generate`. A migration that drops something needs --allow-destructive when it is applied.",
    ),
    "mig.schema_op": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "This changes something wreath's models cannot describe yet (a check or exclusion constraint, a constraint whose kind the call does not name, or an argument that is not a literal). Either move the object onto the model, or leave this revision in Alembic.",
    ),
    "mig.rename": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "A rename is the one ordinary-looking migration that goes wrong on its own. wreath compares the shape of the models with the shape of the database, and a renamed table or column looks exactly like one thing dropped and another created -- which would throw the data away. Keep this revision in Alembic, or do the rename directly in the database first.",
    ),
    "mig.index_manual": (
        "migration_op",
        "other",
        UNSUPPORTED,
        "wreath's migrations cover plain btree indexes. This one is an expression, partial, covering or non-btree index, and it would be written out as an operation that cannot actually be applied. Keep it in Alembic.",
    ),
    "mig.unmodelled_type": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "This column's type has no equivalent in wreath's ORM (Time, Interval, Enum, INET, TSVECTOR and a fixed-width CHAR are the usual ones), so nothing on the model can produce it. Either pick a type wreath does model, or keep this table in Alembic.",
    ),
    "mig.raw_sql": (
        "migration_op",
        "other",
        UNSUPPORTED,
        "op.execute() runs SQL nobody can derive from a model. Keep this revision in Alembic.",
    ),
    # Deferred data migrations shipped (design 24), so "keep it in Alembic" stopped
    # being true. The verdict stays needs-review because the *body* is bespoke —
    # a Recode wants the old->new mapping written out, which is the thing the
    # `op.execute(UPDATE ...)` in this revision encodes and a differ cannot read.
    "mig.data": (
        "migration_op",
        "other",
        NEEDS_REVIEW,
        "op.get_bind() means this revision rewrites rows, not just the schema -- the kind of migration that holds a deploy open for an hour on a large table. Wreath does this without the outage: declare Recode(Model.col, mapping={...}) next to the model for a change of values in place, or Retype for a change of type (new column, backfill, verify, swap), and drive it with jobs.drive(). The app starts and serves immediately while the rows convert in chunks, and wreath refuses a later migration that would narrow the column too early. The mapping is the part only you can write.",
    ),
    # -- caching --------------------------------------------------------------
    "cache.store": (
        "cache",
        "other",
        TRANSLATED,
        "TTLCache and LRUCache become BoundedCache(max_entries=..., ttl=...) from wreath.cache: the same bounded cache with the same eviction, counted against the framework's memory budget. If this caches a table that rarely changes, SnapshotCache with refresh_on() fits better -- but that is a change of approach, not a rename.",
    ),
    "cache.decorator": (
        "cache",
        "other",
        NEEDS_REVIEW,
        "@cachetools.cached becomes @cached(ttl=..., invalidate_on=[Model]) from wreath.response_cache. Naming the models is worth doing: a TTL is a guess, but the ORM announces its writes, so the cache can clear the moment the data changes. cache.invalidate_across_workers(bus) extends that to every worker.",
    ),
    # -- time -----------------------------------------------------------------
    #
    # `wreath.temporal` shipped, so arrow stops being a dependency you have to
    # replace with hand-rolled stdlib and becomes a rename. The catalog said "do
    # not wait for it" while it was designed-not-shipped; leaving that in place
    # once it landed would tell porters to write the code wreath now owns.
    "time.arrow": (
        "time",
        "other",
        TRANSLATED,
        "arrow becomes wreath.temporal, one call at a time: arrow.utcnow() and arrow.now() are temporal.now(), arrow.get(s) is temporal.parse(s), and .humanize() is temporal.relative(value). What you get back is a datetime subclass, so it stores, compares and serializes with no conversion -- and it refuses to be timezone-naive, which is the bug arrow's implicit UTC hides.",
    ),
    "time.arrow_other": (
        "time",
        "other",
        NEEDS_REVIEW,
        "This arrow call has no direct replacement. wreath.temporal covers the clock, parsing and relative wording. What it will not do is shift by months or years, because that is not a fixed number of seconds -- so if that is what this does, say which behaviour you meant.",
    ),
    # -- responses ------------------------------------------------------------
    "resp.class": (
        "response",
        "other",
        TRANSLATED,
        "The response class becomes the wreath one of the same name (PlainTextResponse is TextResponse). Two argument names differ: content= is the first argument, and status_code= is status=.",
    ),
    "resp.status_const": (
        "response",
        "other",
        TRANSLATED,
        "status.HTTP_404_NOT_FOUND is just 404. Where it is raised, the wreath exception class says it better: raise NotFound().",
    ),
    "resp.jsonable": (
        "response",
        "other",
        TRANSLATED,
        "Delete the jsonable_encoder() wrapper. wreath's JSON encoder already handles dataclasses, database rows, UUIDs and datetimes.",
    ),
    "route.response_class": (
        "route_option",
        "other",
        NEEDS_REVIEW,
        "Delete response_class= and return that response type from the handler instead. Wreath picks the response from what you return.",
    ),
    # -- auth schemes ---------------------------------------------------------
    "auth.security_scheme": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "Wreath authenticates once at the route boundary rather than through a dependency on each route. Configure it with configure_auth(BearerTokenBackend(...)) or ApiKeyBackend(...), and delete the scheme object -- routes stop declaring it.",
    ),
    "auth.security": (
        "auth",
        "other",
        NEEDS_REVIEW,
        "Security(scheme, scopes=[...]) splits in two: the dependency becomes a plain Depends(), and the scopes become @permissions(...) or @roles(...) on the route. Wreath has no scope slot on the dependency itself.",
    ),
    # -- the test suite -------------------------------------------------------
    "test.client": (
        "test",
        "other",
        NEEDS_REVIEW,
        "wreath.testing.TestClient is async. Three changes: open it with `async with TestClient(app) as client`, await every request, and read response.status instead of response.status_code.",
    ),
    "test.client_local": (
        "test",
        "other",
        TRANSLATED,
        "This function-local client has an exact async lifetime: make the test async, open the client with `async with`, await its requests, and use response.status. Pass --opinionated and the porter writes all four changes together.",
    ),
    "test.dependency_override": (
        "test",
        "other",
        NEEDS_REVIEW,
        'There is no dependency_overrides in wreath. Authentication becomes client.acting_as("principal", roles=[...]); an outbound service becomes a registered ServiceClient over a test transport adapter; database tests keep the same Session and select a real or replay PostgreSQL adapter underneath it. Delete fake repositories instead of porting or injecting them.',
    ),
    # -- libraries that are not framework features ----------------------------
    "ext.pandas": (
        "external",
        "other",
        UNSUPPORTED,
        "This is data analysis, not framework code. Keep pandas and leave the module as it is.",
    ),
    "ext.httpx": (
        "external",
        "other",
        NEEDS_REVIEW,
        "Register one managed HTTPClient with app.http_client(...), then expose calls through a ServiceClient (or a generated typed ServiceClient). The app owns connection lifetime, rate limits, retries, origin pinning, and transport adapters; do not reproduce httpx response semantics as a compatibility layer.",
    ),
    # -- confidence -----------------------------------------------------------
    "resolve.star_import": (
        "star_import",
        "other",
        NEEDS_REVIEW,
        "This module uses `from ... import *`, so this tool cannot always tell where a name came from. Anything it reported here is less certain than usual; the quickest fix is to import the names you use.",
    ),
}


def rule(rule_id: str) -> tuple[str, str, str, str]:
    return RULES[rule_id]
