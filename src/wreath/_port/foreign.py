"""Constructs from frameworks this tool does not translate.

An analyzer that only knows FastAPI reports a Tornado application as
``0 translated, 0 needs-review, 0 unsupported`` — the same three numbers an
empty directory produces. That is the worst available answer: it reads as
"nothing to do" when the truth is "everything to do, and I cannot help with any
of it".

So foreign constructs are *recognized* here and tagged `unsupported`. Recognized
is not translated: the point is to put a number on the work rather than to do
it. A Tornado tree with fourteen handlers should say fourteen, so the size of
the job is visible before anyone commits to it.

The exception is Django, and it is deliberate rather than a slope. A Django
field whose storage wreath matches exactly *is* a `column(PgType, ...)`, a model
that is nothing but fields *is* a class-header rename, and
`with transaction.atomic():` *is* `async with session.begin():`. Those live here
because this module owns Django recognition, and recognizing them in one place
is what stops the report and the emitted file disagreeing about a line. Every
one of them is refused by name the moment the source stops being exactly that
shape -- an unmapped field type, a manager, a `save()` override, an
`atomic(using=...)`.

Two things this file learned the hard way:

* **Gate every rule on the framework's import.** `@app.route(...)` is spelled
  identically in Flask and Bottle, and aiohttp's `@routes.get` is FastAPI's
  exactly. Without the gate these rules fire on each other's code.
* **Class hierarchies cross modules.** Almost nothing subclasses
  `tornado.web.RequestHandler` directly — a tree declares one `BaseHandler` in
  `handlers/base.py` and inherits it by import everywhere else. Resolving bases
  per module found *one* handler in a tree holding fourteen, so the family
  resolution runs over the whole tree's class map.
"""

from __future__ import annotations

import ast

from .frameworks import REDIRECT_STATUS, raised_exception, route_methods, wreath_path
from .ir import Finding
from .rules import RULES

# Django field -> the wreath column type that means the same thing. Mirrors
# `_ORMAR_TYPE`: a field with no entry here is refused with a note rather than
# guessed at, because a column that stores the wrong width is worse than one a
# human had to map. Django's own docs give the storage for each of these, and
# these are the ones postgres and wreath agree on exactly.
_DJANGO_TYPE = {
    "AutoField": "Int32",
    "BigAutoField": "Int64",
    "BigIntegerField": "Int64",
    "BinaryField": "Bytea",
    "BooleanField": "Bool",
    "CharField": "Varchar",
    "DateField": "Date",
    # Django writes an aware datetime under USE_TZ and wreath refuses a naive
    # one outright, so TimestampTz is the only target that keeps the contract.
    "DateTimeField": "TimestampTz",
    "DecimalField": "Numeric",
    "EmailField": "Varchar",
    "FloatField": "Float64",
    "IntegerField": "Int32",
    "JSONField": "Jsonb",
    "PositiveBigIntegerField": "Int64",
    "PositiveIntegerField": "Int32",
    "PositiveSmallIntegerField": "Int16",
    "SlugField": "Varchar",
    "SmallAutoField": "Int16",
    "SmallIntegerField": "Int16",
    "TextField": "Text",
    "URLField": "Varchar",
    "UUIDField": "Uuid",
}

# Django field keywords that describe the field for the admin, forms or a
# migration's own bookkeeping. Wreath has nowhere to put them and nothing
# depends on them, so they go without a note -- the same treatment ormar's
# `description=` gets, and for the same reason: a note on every column buries
# the ones that matter.
_DJANGO_DOC_KWARGS = frozenset({
    "blank", "verbose_name", "help_text", "editable", "error_messages",
    "related_name", "related_query_name", "validators", "choices", "db_comment",
})


_HTTP = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "route"})
_HANDLER_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_HANDLER_HOOKS = frozenset({"prepare", "on_finish", "initialize", "on_connection_close"})

#: Dunders that are a framework contract rather than Python's own. Pyramid
#: locates a resource by walking `__parent__`/`__name__`, so skipping every
#: dunder skipped the traversal contract itself.
_CONTRACT_DUNDERS = frozenset({"__parent__", "__name__", "__acl__", "__getitem__"})

_TORNADO_HANDLERS = ("RequestHandler",)
_TORNADO_SOCKETS = ("WebSocketHandler",)
_DRF_BASES = ("ViewSet", "APIView", "GenericAPIView", "Serializer", "ModelSerializer")

_FLASK_HOOKS = frozenset({
    "before_request", "after_request", "teardown_request", "errorhandler",
    "before_app_request", "context_processor", "teardown_appcontext",
})
#: Parameter names that are the framework's own objects in a handler or view.
#: A convention rather than a resolution: `request` in an aiohttp handler is a
#: web.Request and nothing in the signature says so. Scoped to parameters of a
#: function in a module that imports the framework, so a local named `request`
#: in unrelated code cannot trip it.
_FRAMEWORK_PARAMS = {
    "aiohttp": ("request", "app"),
    "pyramid": ("request", "context", "config"),
}

#: Blocking I/O under a monkeypatch. Not frameworks -- but in a patched tree
#: these are the calls whose semantics the patch rewrote, and the reason the
#: tree cannot be ported mechanically. psycopg2 is the sharpest case: a C
#: extension that never yields, so it blocks the whole hub.
_BLOCKING_ROOTS = frozenset({"requests", "psycopg2", "urllib", "urllib3", "socket", "httplib"})

_AIOHTTP_LIFECYCLE = frozenset({"on_startup", "on_cleanup", "on_shutdown", "cleanup_ctx"})
_AIOHTTP_RESPONSES = frozenset({"Response", "json_response", "StreamResponse", "FileResponse"})


def _base_names(node: ast.ClassDef) -> list[str]:
    out = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def _callee(node: ast.expr) -> str:
    """Trailing name of whatever is being called or decorated."""
    if isinstance(node, ast.Call):
        return _callee(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


_DJANGO_FAMILY = ("Model", "Manager", "QuerySet", "Serializer", "ViewSet", "APIView")

#: Bases whose descendants are a query surface rather than a row.
_DJANGO_MANAGERS = ("Manager", "QuerySet")

#: Model methods Django calls on every write. Overriding one puts application
#: logic on the persistence path, and wreath's model declaration has no slot for
#: it -- so the class stops being a header rename however plain its fields are.
_DJANGO_PERSISTENCE = frozenset({"save", "delete"})


def django_manager_callees(node: ast.ClassDef) -> tuple[str, ...]:
    """What each class-level assignment in a model body calls.

    `objects = ActiveManager()` is how a manager is attached, and the callee is
    the only part of it worth reading -- whether `ActiveManager` *is* a manager
    is a tree-wide question its own module cannot answer.
    """
    return tuple(
        _callee(stmt.value)
        for stmt in node.body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Call)
    )


def django_overrides_persistence(node: ast.ClassDef) -> bool:
    """Whether this class body overrides `save` or `delete`."""
    return any(
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        and stmt.name in _DJANGO_PERSISTENCE
        for stmt in node.body
    )


def blueprint_router(call: ast.Call) -> tuple[str, str | None] | None:
    """`("plots", "/plots")` for `Blueprint("plots", __name__, url_prefix="/plots")`.

    `None` when the name is not a literal, or when the blueprint carries any
    option beyond the prefix -- `template_folder`, `static_folder` and
    `subdomain` all describe things wreath's `Router` does not do, and dropping
    one silently is how a port loses a whole static mount.
    """
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return None
    name = call.args[0].value
    if not isinstance(name, str):
        return None
    prefix: str | None = None
    for keyword in call.keywords:
        if keyword.arg != "url_prefix":
            return None
        if not (
            isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return None
        prefix = keyword.value.value
    return name, prefix


def _blueprint_translates(
    call: ast.Call, bound_to: str | None, hooked: frozenset[str]
) -> bool:
    """A Blueprint is a `Router` only while it is nothing but routes.

    `url_prefix` and the name carry across. A `before_request` or `errorhandler`
    registered *on the blueprint* does not: wreath's hooks belong to the
    application, so a per-blueprint one changes which requests it runs for.
    `hooked` is the set of names this module saw one registered on, and the
    decorator sits a long way from the `Blueprint(...)` that made the name.
    """
    return blueprint_router(call) is not None and bound_to not in hooked


def assigned_names(tree: ast.Module) -> dict[int, str]:
    """`id(call) -> the single name it was assigned to`, for calls that were."""
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
            out[id(node.value)] = target.id
    return out


def router_hook_owners(tree: ast.Module) -> frozenset[str]:
    """Names decorated as a request hook or error handler somewhere in this module.

    `@plots.before_request` makes `plots` more than a set of routes, and the
    decorator is written a long way from the `Blueprint(...)` that made it.
    """
    owners: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and target.attr in _FLASK_HOOKS
                and isinstance(target.value, ast.Name)
            ):
                owners.add(target.value.id)
    return frozenset(owners)


def route_pattern(dec: ast.expr) -> str | None:
    """The literal path a route decorator registers, if it is written out."""
    if not isinstance(dec, ast.Call) or not dec.args:
        return None
    first = dec.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def route_translates(dec: ast.expr, attr: str, framework: str, node) -> bool:
    """Whether this whole route -- decorator, path and signature -- carries across.

    All three have to, or none of it does. A path whose converter has no wreath
    form (`<uuid:x>`, a regex) leaves the route matching something different; a
    `methods=` built at runtime is not readable; and a capture the handler does
    not declare as a parameter has nowhere to put its annotation. Translating
    two of the three would emit a decorator that no longer agrees with the
    function under it.
    """
    if not isinstance(dec, ast.Call):
        return False
    pattern = route_pattern(dec)
    if pattern is None:
        return False
    if any(
        keyword.arg not in ("methods", "method", "name", "endpoint")
        for keyword in dec.keywords
    ):
        return False
    if route_methods(attr, dec) is None:
        return False
    converted = wreath_path(pattern, framework)
    if converted is None:
        return False
    _new, annotations = converted
    declared = {
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
    }
    # Every capture has to be a parameter this handler declares. aiohttp is the
    # exception by construction -- its handler takes only `request` and reads
    # `match_info` in the body -- which is why an aiohttp route with captures is
    # not this rule.
    return all(name in declared for name in annotations)


def _redirect_status(name: str, call: ast.Call) -> int | None:
    """The status one foreign redirect means, or `None` if this is not one.

    Two shapes. A named class (`HTTPFound`, `HttpResponseRedirect`) carries its
    status in the name. Flask's and Bottle's `redirect(url, code)` carries it in
    an argument -- and its *default* is 302 in both, where wreath's
    `RedirectResponse` defaults to 307, so an omitted code is 302 rather than
    "no status to carry".
    """
    named = REDIRECT_STATUS.get(name)
    if named is not None:
        return named
    if name != "redirect" or not call.args:
        return None
    given = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "code"), None
    )
    if given is None and len(call.args) > 1:
        given = call.args[1]
    if given is None:
        return 302
    if isinstance(given, ast.Constant) and isinstance(given.value, int):
        return None if isinstance(given.value, bool) else given.value
    return None


def redirect_target(name: str, call: ast.Call) -> ast.expr | None:
    """The URL expression a foreign redirect sends the client to."""
    if call.args:
        return call.args[0]
    return next(
        (
            keyword.value
            for keyword in call.keywords
            if keyword.arg in ("location", "url", "redirect_to")
        ),
        None,
    )


def is_atomic_block(node: ast.Call, imports) -> bool:
    """Whether this call is a bare `transaction.atomic()` opening a block.

    Only the bare form. `atomic(using="replica")` picks a database, which is a
    `Session` of its own in wreath rather than an argument to the block, and
    `@transaction.atomic` as a decorator wraps a whole function -- neither is
    the one-line substitution `async with session.begin()` is.
    """
    return (
        not node.args
        and not node.keywords
        and imports.origin(node.func) == "django.db.transaction.atomic"
    )


def model_carries_behaviour(node: ast.ClassDef, manager_family: set[str]) -> bool:
    """Whether anything on this model has no declarative form in wreath.

    Two things: a manager, whose `get_queryset()` is a predicate on every
    `.objects` call and appears at none of them; and an override of `save` or
    `delete`, which is application logic on the write path. Either one makes
    `class X(Model)` a port rather than a rename -- and makes `X.objects`
    something other than every row.
    """
    return django_overrides_persistence(node) or any(
        callee in _DJANGO_MANAGERS or callee in manager_family
        for callee in django_manager_callees(node)
    )


def resolve_family(class_bases: dict[str, list[str]], markers: tuple[str, ...]) -> set[str]:
    """Class names descending from a framework base, to a fixpoint.

    Tree-wide and by name: real applications declare one base and inherit it
    by import, so any per-module answer is wrong for most of the tree.
    """
    family: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, bases in class_bases.items():
            if name in family:
                continue
            if any(b.endswith(markers) or b in family for b in bases):
                family.add(name)
                changed = True
    return family


def tornado_families(class_bases: dict[str, list[str]]) -> tuple[set[str], set[str]]:
    """Which class names in the tree are request handlers, and which are sockets.

    Runs to a fixpoint over the tree-wide map, so a chain of any depth resolves:
    `UmpireConsoleHandler` → `AuthedMixin`, `BaseHandler` → `RequestHandler`,
    spread across three files.
    """
    handlers: set[str] = set()
    sockets: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, bases in class_bases.items():
            if name in sockets or name in handlers:
                continue
            if any(b.endswith(_TORNADO_SOCKETS) or b in sockets for b in bases):
                sockets.add(name)
                changed = True
            elif any(b.endswith(_TORNADO_HANDLERS) or b in handlers for b in bases):
                handlers.add(name)
                changed = True
    return handlers, sockets


#: Root package -> the catch-all rule for anything of its API without a rule of
#: its own. Hand-written rules will never be complete -- `ClientTimeout`,
#: `RouteTableDef`, `AppRunner`, `set_security_policy` and view predicates were
#: all sitting in plain sight after two rounds of enumerating by hand. Resolving
#: names through the import table instead makes coverage a property of the
#: design rather than of how much of the framework someone remembered.
_CATCH_ALL = {
    "flask": "foreign.flask.api",
    "aiohttp": "foreign.aiohttp.api",
    "tornado": "foreign.tornado.api",
    "pyramid": "foreign.pyramid.api",
    "bottle": "foreign.bottle.api",
    "django": "foreign.django.api",
    "gevent": "foreign.gevent.api",
    "eventlet": "foreign.gevent.api",
}


#: Nodes that open a scope of their own. An assignment inside one binds a name
#: the enclosing scope cannot see, and a class body binds a name *no* other
#: scope can see -- it is reached through the class, never as a bare name.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _assignment_aliases(scope: ast.AST, imports) -> dict[str, str]:
    """Names this scope itself binds to a framework object by assignment.

    `Response = web.Response` leaves every later `Response(...)` invisible to
    the import table, which only knows what an import statement said.

    Collected per scope rather than per module, because a name is only an alias
    where it is in scope. Walking the whole module collected class attributes
    too, so a Django model with a `beach = models.CharField(...)` field taught
    every later local named `beach` that it was Django API -- a finding on a
    line that touches no framework at all.
    """
    aliases: dict[str, str] = {}

    def collect(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                continue  # its assignments belong to its own scope
            if isinstance(child, ast.Assign) and len(child.targets) == 1:
                target = child.targets[0]
                value = child.value.func if isinstance(child.value, ast.Call) else child.value
                if isinstance(target, ast.Name) and isinstance(value, (ast.Name, ast.Attribute)):
                    origin = imports.origin(value)
                    if origin.split(".")[0] in _CATCH_ALL:
                        aliases[target.id] = origin
            collect(child)

    collect(scope)
    return aliases


def _api_references(tree: ast.Module, imports, roots: frozenset[str]) -> list[tuple[str, int, str]]:
    """Every reference to a foreign framework's API, by import resolution.

    Walks outermost-first and consumes the inside of each attribute chain, so
    `web.Response(...)` is one reference rather than one for `web` and one for
    `web.Response`.
    """
    out: list[tuple[str, int, str]] = []
    consumed: set[int] = set()

    def visit(node: ast.AST, aliases: dict[str, str]) -> None:
        if id(node) in consumed:
            return
        if isinstance(node, (ast.Attribute, ast.Name)):
            origin = imports.origin(node)
            if isinstance(node, ast.Name) and node.id in aliases:
                origin = aliases[node.id]
            root = origin.split(".")[0]
            if root in _CATCH_ALL and root in roots:
                out.append((_CATCH_ALL[root], node.lineno, origin))
                for inner in ast.walk(node):
                    consumed.add(id(inner))
                return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # A function sees its own bindings and its enclosing ones; a class
            # body's names are not in scope for anything nested inside it, so a
            # ClassDef descends with the aliases it inherited and adds none.
            aliases = {**aliases, **_assignment_aliases(node, imports)}
        for child in ast.iter_child_nodes(node):
            visit(child, aliases)

    visit(tree, _assignment_aliases(tree, imports))
    return out


def _declared_through(
    name: str, class_bases: dict[str, list[str]], class_members: dict[str, set[str]]
) -> set[str]:
    """Every name declared by a class or any ancestor this codebase owns.

    Stops at bases it has never seen -- those are the framework's, and their
    members are exactly what this is trying to identify. Cycles are possible
    in a malformed tree, so the walk tracks what it has visited.
    """
    seen: set[str] = set()
    out: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        out |= class_members.get(current, set())
        stack.extend(class_bases.get(current, ()))
    return out


def foreign_findings(
    rel: str,
    tree: ast.Module,
    roots: frozenset[str],
    class_bases: dict[str, list[str]] | None = None,
    imports=None,
    class_members: dict[str, set[str]] | None = None,
) -> list[Finding]:
    """Recognized-but-unportable constructs in one module.

    `roots` is the set of top-level packages the module imports; a rule fires
    only for a framework named there. `class_bases` is the tree-wide class map,
    needed because handler hierarchies span files.
    """
    emitted: list[Finding] = []
    seen: set[tuple[str, int]] = set()

    def emit(rule_id: str, line: int) -> None:
        if (rule_id, line) in seen:
            return
        seen.add((rule_id, line))
        construct, category, tag, message = RULES[rule_id]
        emitted.append(Finding(rel, line, construct, tag, rule_id, message, category))

    flask = "flask" in roots
    aio = "aiohttp" in roots
    tornado = "tornado" in roots
    pyramid = "pyramid" in roots
    bottle = "bottle" in roots
    django = "django" in roots
    patched = bool(roots & {"gevent", "eventlet"})
    #: Whether this module speaks any HTTP framework at all. The shared
    #: construct rules (`port.http.*`) are gated on it rather than on one
    #: framework, because they are the constructs all five spell differently and
    #: mean identically -- but they are still gated, or `abort(404)` from an
    #: unrelated library would be read as Flask's.
    web = flask or aio or tornado or pyramid or bottle or django
    if not (web or patched):
        return emitted

    local = {n.name: _base_names(n) for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    all_bases = {**local, **(class_bases or {})}
    django_family = resolve_family(all_bases, _DJANGO_FAMILY) if django else set()
    manager_family = resolve_family(all_bases, _DJANGO_MANAGERS) if django else set()

    def _emit_django_columns(node: ast.ClassDef) -> None:
        """Verdict per field on a Django model.

        Exactly the discipline the ormar path already uses: a field whose
        storage wreath matches is translated, and one it does not is refused by
        name. Nothing here invents a column type to make a number move.
        """
        for stmt in node.body:
            value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
            if not isinstance(value, ast.Call):
                continue
            tail = _callee(value)
            if tail in _DJANGO_TYPE:
                emit("orm.django.column", stmt.lineno)
            elif tail in ("ForeignKey", "OneToOneField"):
                emit("orm.django.fk", stmt.lineno)
            elif tail == "ManyToManyField":
                emit("orm.django.m2m", stmt.lineno)
            elif tail.endswith("Field"):
                emit("orm.django.column_unmapped", stmt.lineno)

    def _emit_inherited(node: ast.ClassDef, rule_id: str) -> None:
        """Framework API reached through `self`, which no import mentions.

        Anything this class calls on itself that neither it nor an ancestor
        this codebase owns declares was inherited from the framework base.
        No API list needed: the ancestry walk stops at the bases it has never
        seen, and those are precisely the framework's.
        """
        declared = _declared_through(node.name, all_bases, class_members or {})
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "self"
                and inner.attr not in declared
                and (not inner.attr.startswith("__") or inner.attr in _CONTRACT_DUNDERS)
            ):
                emit(rule_id, inner.lineno)

    handlers, sockets = tornado_families(all_bases)
    # `transaction.atomic()` is only the block wreath spells `session.begin()`
    # where it opens one. Called anywhere else it is the decorator or a context
    # manager somebody stored, and neither is that substitution.
    hooked_routers = router_hook_owners(tree)
    bound_names = assigned_names(tree)
    atomic_blocks = {
        id(item.context_expr)
        for statement in ast.walk(tree)
        if isinstance(statement, (ast.With, ast.AsyncWith))
        for item in statement.items
        if isinstance(item.context_expr, ast.Call)
    }

    for node in ast.walk(tree):
        # -- decorators ------------------------------------------------------
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                name = _callee(dec)
                dotted = _dotted(dec.func if isinstance(dec, ast.Call) else dec)
                line = getattr(dec, "lineno", node.lineno)
                if name in _HTTP:
                    framework = (
                        "flask" if flask else "bottle" if bottle else "aiohttp" if aio else ""
                    )
                    if framework and not patched and route_translates(
                        dec, name, framework, node
                    ):
                        emit("port.route.method", line)
                    elif flask:
                        emit("foreign.flask.route", line)
                    elif bottle:
                        emit("foreign.bottle.route", line)
                    elif aio:
                        emit("foreign.aiohttp.route", line)
                elif flask and name in _FLASK_HOOKS:
                    emit("foreign.flask.hook", line)
                elif aio and name == "middleware":
                    emit("foreign.aiohttp.middleware", line)
                elif tornado and dotted.endswith("gen.coroutine"):
                    emit("foreign.tornado.coroutine", line)
                elif tornado and name == "authenticated":
                    emit("foreign.tornado.authenticated", line)
                elif pyramid and name in ("view_config", "subscriber", "view_defaults"):
                    emit("foreign.pyramid.view", line)

        # -- classes ---------------------------------------------------------
        if isinstance(node, ast.ClassDef):
            bases = _base_names(node)
            is_socket = tornado and node.name in sockets
            is_handler = tornado and node.name in handlers
            if is_socket:
                emit("foreign.tornado.websocket", node.lineno)
            elif is_handler:
                emit("foreign.tornado.handler", node.lineno)
            if is_socket or is_handler:
                # The verbs are the routes. A handler with get/post/delete is
                # three endpoints, and counting the class alone hides two of them.
                for child in node.body:
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if child.name in _HANDLER_METHODS:
                        emit("foreign.tornado.method", child.lineno)
                    elif child.name in _HANDLER_HOOKS:
                        emit("foreign.tornado.hook", child.lineno)
                # `self.write(...)` is Tornado's API and no import mentions it.
                # Anything this class calls on itself that neither it nor any of
                # its local ancestors declares came from the base class -- which,
                # for a class in this family, is the framework.
                _emit_inherited(node, "foreign.tornado.inherited")
            if django:
                if node.name in django_family:
                    _emit_inherited(node, "foreign.django.inherited")
                if any(b.endswith("Model") for b in bases):
                    # The fields are billed either way -- what splits is the
                    # class. `orm.django.model` promises a header rename, and
                    # that promise only holds for a model that is fields.
                    emit(
                        "foreign.django.model"
                        if model_carries_behaviour(node, manager_family)
                        else "orm.django.model",
                        node.lineno,
                    )
                    _emit_django_columns(node)
                elif any(b in ("Manager", "QuerySet") for b in bases):
                    emit("foreign.django.manager", node.lineno)
                elif any(b.endswith(_DRF_BASES) for b in bases):
                    emit("foreign.django.drf", node.lineno)
            if pyramid:
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "__getitem__":
                        emit("foreign.pyramid.traversal", child.lineno)
                    elif isinstance(child, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "__acl__" for t in child.targets
                    ):
                        emit("foreign.pyramid.acl", child.lineno)

        # -- assignments -----------------------------------------------------
        if isinstance(node, ast.Assign) and pyramid:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__acl__":
                    emit("foreign.pyramid.acl", node.lineno)

        # -- calls -----------------------------------------------------------
        if isinstance(node, ast.Call):
            name = _callee(node)
            dotted = _dotted(node.func)
            if web and not patched and raised_exception(name, node) is not None:
                # Every framework here spells an HTTP error differently and they
                # all mean `raise <Class>()`. Refused under a monkeypatch with
                # everything else in that tree: a rewrite that looks like it
                # worked is worse there than one that did not happen.
                emit("port.http.exception", node.lineno)
            elif web and not patched and _redirect_status(name, node) is not None:
                emit("port.http.redirect", node.lineno)
            elif patched and name in ("patch_all", "monkey_patch"):
                emit("foreign.gevent.monkeypatch", node.lineno)
            elif patched and name in ("spawn", "spawn_later"):
                emit("foreign.gevent.spawn", node.lineno)
            elif patched and name in ("Pool", "Group"):
                emit("foreign.gevent.pool", node.lineno)
            elif patched and name == "Timeout":
                emit("foreign.gevent.timeout", node.lineno)
            elif patched and dotted.startswith("threading.") and name in ("local", "Lock", "RLock"):
                emit("foreign.gevent.threading", node.lineno)
            elif patched and name == "Session" and "requests" in dotted:
                emit("foreign.gevent.session", node.lineno)
            elif flask and name == "Flask":
                emit("port.app.wreath", node.lineno)
            elif flask and name == "Blueprint":
                emit(
                    "port.router.new"
                    if _blueprint_translates(
                        node, bound_names.get(id(node)), hooked_routers
                    )
                    else "foreign.flask.blueprint",
                    node.lineno,
                )
            elif bottle and not patched and name == "Bottle":
                emit("port.app.wreath", node.lineno)
            elif bottle and name == "Bottle":
                emit("foreign.bottle.app", node.lineno)
            elif aio and not patched and name == "RouteTableDef":
                emit("port.router.new", node.lineno)
            elif name in ("register_blueprint", "add_routes") and (flask or aio) \
                    and not patched and len(node.args) == 1 and not node.keywords:
                emit("port.router.include", node.lineno)
            elif aio and name.startswith("add_") and ".router." in f"{dotted}.":
                emit("foreign.aiohttp.route_dynamic", node.lineno)
            elif aio and name == "add_subapp":
                emit("foreign.aiohttp.subapp", node.lineno)
            elif aio and name == "Application" and "web" in dotted:
                emit("foreign.aiohttp.app", node.lineno)
            elif aio and name == "ClientSession":
                emit("foreign.aiohttp.client", node.lineno)
            elif aio and name in _AIOHTTP_RESPONSES and "web" in dotted:
                emit("foreign.aiohttp.response", node.lineno)
            elif tornado and name == "Application":
                emit("foreign.tornado.routes", node.lineno)
            elif tornado and name in ("PeriodicCallback", "add_timeout", "call_later"):
                emit("foreign.tornado.periodic", node.lineno)
            elif tornado and name == "define" and (
                "options" in dotted
                or (imports is not None
                    and imports.origin(node.func).startswith("tornado.options"))
            ):
                # `from tornado.options import define` is the spelling Tornado's
                # own documentation uses, and it leaves nothing but `define` at
                # the call site -- so reading the dotted *text* billed a
                # process-wide configuration surface as unremarkable API. The
                # import table already knows where the name came from.
                emit("foreign.tornado.options", node.lineno)
            elif pyramid and name in ("add_route", "add_view"):
                emit("foreign.pyramid.route", node.lineno)
            elif pyramid and name == "add_tween":
                emit("foreign.pyramid.tween", node.lineno)
            elif pyramid and name == "include":
                emit("foreign.pyramid.include", node.lineno)
            elif pyramid and name == "Configurator":
                emit("foreign.pyramid.config", node.lineno)
            elif django and dotted.endswith("site.register"):
                emit("foreign.django.admin", node.lineno)
            elif (
                django
                and imports is not None
                and id(node) in atomic_blocks
                and is_atomic_block(node, imports)
            ):
                emit("orm.transaction.atomic", node.lineno)

        # -- attribute reads --------------------------------------------------
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if aio and node.attr in _AIOHTTP_LIFECYCLE:
                emit("foreign.aiohttp.lifecycle", node.lineno)
            elif pyramid and node.attr == "registry":
                emit("foreign.pyramid.registry", node.lineno)
            elif flask and dotted.startswith(("g.", "current_app.")):
                emit("foreign.flask.proxy", node.lineno)

        # -- subscripted app state -------------------------------------------
        if (
            aio
            and isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("app", "application", "request")
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            emit("foreign.aiohttp.state", node.lineno)

    # Framework objects that arrive as parameters. `request` in an aiohttp
    # handler is a web.Request; no import in the module mentions it, and the
    # import table cannot resolve a parameter. Restricted to parameters of the
    # enclosing function so an unrelated local of the same name is untouched.
    param_names: set[str] = set()
    for framework, names in _FRAMEWORK_PARAMS.items():
        if framework in roots:
            param_names.update(names)
    if param_names:
        rule = "foreign.aiohttp.request" if aio else "foreign.pyramid.request"
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            bound = {
                a.arg for a in list(func.args.args) + list(func.args.posonlyargs)
                + list(func.args.kwonlyargs) if a.arg in param_names
            }
            if not bound:
                continue
            for inner in ast.walk(func):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id in bound
                ):
                    emit(rule, inner.lineno)

    # Blocking I/O beneath a monkeypatch. These are not framework calls, and in
    # a patched tree they are the whole problem: the patch decided whether they
    # yield, and psycopg2 -- a C extension -- decided not to.
    if patched and imports is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Attribute, ast.Name)):
                if imports.origin(node).split(".")[0] in _BLOCKING_ROOTS:
                    emit("foreign.gevent.blocking", node.lineno)

    # Everything else this module touches of a framework it cannot port. The
    # named rules above carry the message that matters for the constructs worth
    # a specific warning; this is what stops the count being a function of how
    # many of them someone thought to write.
    if imports is not None:
        specific = {line for _, line in seen}
        for rule_id, line, origin in _api_references(tree, imports, roots):
            if line in specific:
                continue
            construct, category, tag, message = RULES[rule_id]
            key = (rule_id, line)
            if key in seen:
                continue
            seen.add(key)
            emitted.append(
                Finding(rel, line, construct, tag, rule_id, f"{message} ({origin})", category)
            )

    return emitted
