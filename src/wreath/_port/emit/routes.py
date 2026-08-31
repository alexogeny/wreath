"""Route handlers: the signature -- request, session, parameter markers -- and
the options on the decorator above it."""

from __future__ import annotations

import ast

from ..analyzer import (
    HTTP_METHODS,
    _is_false,
    _returns_in,
    redundant_literal_validator,
    response_class_rule,
    status_code_rule,
    status_int,
)
from .frameworks import _ForeignRewrite
from .jobs import _BackgroundWork
from .session import _SessionThreading
from .targets import (
    _KW_KEEP,
    _KW_RENAME,
    _MARKER_DOC_KWARGS,
    _MARKERS,
    _SESSION_PARAM,
    _STATUS_WRAPPER,
)
from .testing import _TestClient


def _marker_default(call: ast.Call) -> ast.expr | None:
    """The default value a `Query(...)`/`Path(...)` marker carries, if any.

    FastAPI accepts it either way round — `Query(20)` and `Query(default=20)`
    are the same parameter — and reading only the positional spelling made every
    `Query(default=False)` look like a *required* parameter, which is the
    opposite of what it says.
    """
    for keyword in call.keywords:
        if keyword.arg == "default":
            return keyword.value
    if call.args and not (
        isinstance(call.args[0], ast.Constant) and call.args[0].value is Ellipsis
    ):
        return call.args[0]
    return None


class _RouteRewrite(_SessionThreading, _TestClient, _BackgroundWork, _ForeignRewrite):
    def visit_FunctionDef(self, node) -> None:
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "task"
                and isinstance(target.value, ast.Name)
                and target.value.id in self._celery_runners
            ):
                self._rewrite_celery_task(node, dec, target.value.id)
        if redundant_literal_validator(node, self._parents, self.imports):
            start = self.buf.start_of_line(node.decorator_list[0].lineno)
            end = self.buf.end_of(node)
            self.buf._edits.append((start, end, b""))
            self._replaced.append((start, end))
            self._removed_pydantic_imports.add("field_validator")
            return
        outer_test_clients = self._test_clients
        fixture_clients = {
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if argument.arg in self._fixture_test_clients
        }
        referenced_globals = {
            name
            for name in self._global_test_clients
            if any(isinstance(inner, ast.Name) and inner.id == name for inner in ast.walk(node))
        }
        if node.name.startswith("test_"):
            for name in sorted(referenced_globals):
                self._add_test_fixture_param(node, name)
            fixture_clients |= referenced_globals
        if fixture_clients and isinstance(node, ast.FunctionDef):
            self.buf._edits.append((self.buf.start_of(node), self.buf.start_of(node), b"async "))
        self._rewrite_test_client_fixture(node)
        for inner in ast.walk(node):
            if not isinstance(inner, (ast.With, ast.AsyncWith)):
                continue
            for item in inner.items:
                if not (
                    isinstance(item.context_expr, ast.Call)
                    and self._is_test_client_call(item.context_expr)
                ):
                    continue
                if isinstance(inner, ast.With):
                    self.buf._edits.append(
                        (self.buf.start_of(inner), self.buf.start_of(inner), b"async ")
                    )
                    if isinstance(node, ast.FunctionDef) and not fixture_clients:
                        self.buf._edits.append(
                            (self.buf.start_of(node), self.buf.start_of(node), b"async ")
                        )
                if isinstance(item.optional_vars, ast.Name):
                    fixture_clients.add(item.optional_vars.id)
                self._resolve(item.context_expr.lineno, "test.client")
        self._test_clients |= fixture_clients
        rewritten_test_clients = self._rewrite_test_client_function(node)
        if rewritten_test_clients:
            self._test_clients |= rewritten_test_clients
        route_dec = None
        for dec in node.decorator_list:
            attr = (
                dec.func.attr
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute))
                else None
            )
            if attr in HTTP_METHODS or attr == "route":
                # A foreign route is still a route: its decorator and path move
                # here, and the leading `request: Request` below is the same
                # edit every wreath handler needs. `_foreign_route` answers
                # `False` for a FastAPI one, which falls through unchanged.
                if self._foreign_route(node, dec, attr):
                    self._ensure_request_param(node)
                    continue
            if attr in HTTP_METHODS:
                route_dec = dec
                self._rewrite_route_options(dec, node)
            # A websocket route, a pydantic validator, a lifespan and a Celery
            # task all need a human, and all of them are recognized once by the
            # analyzer — `annotate_findings` writes their notes.
        if route_dec is not None:
            self._rewrite_route_background_tasks(node)
            self._rewrite_route_created_tasks(node)
            session = self._route_session_name(node)
            self._ensure_request_param(
                node,
                self._route_needs_keyword_only(node) or session == _SESSION_PARAM,
                session,
            )
            self._split_markers(node)
            self._rewrite_as_form_params(node)
            outer, self._session = self._session, session
            self.generic_visit(node)
            self._session = outer
            self._test_clients = outer_test_clients
            return
        if node.name in self._dep_targets:
            self._ensure_request_param(node)  # Phase 3: dependency callable gains `request`
            self._rewrite_as_form_params(node)
        # Outside a route handler nothing supplies a session. By default the
        # queries are left where they are and the note goes on the *function* —
        # one decision to make ("this needs a session, and every caller has to
        # pass it") rather than the same sentence over every query in the body.
        # `--opinionated` makes that decision instead: the parameter is added and
        # the queries are written out. It is separated because it is the one
        # rewrite whose effect leaves the file — every call to this function now
        # has to pass a session — and a codemod should not change a signature
        # someone else depends on without being asked.
        outer_session, outer_wanted = self._session, self._session_wanted
        self._session, self._session_wanted = None, False
        existing_session = next(
            (
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                if argument.annotation is not None and self._is_orm_session(argument.annotation)
            ),
            None,
        )
        if existing_session is not None:
            self._session = existing_session
        elif (
            self.opinionated
            and isinstance(node, ast.AsyncFunctionDef)
            and self._session_definition_selected(node)
            and self._can_take_session(node)
        ):
            self._session = _SESSION_PARAM
            self._add_session_param(node)
            # Opinionated mode owns both ends of this signature change: the
            # tree call graph selected the function and every selected call is
            # updated below. A successful rewrite is not review work.
            self._resolve(node.lineno, "orm.query.session_added")
        self.generic_visit(node)
        if self._session_wanted:
            self._annotate(node.lineno, "orm.query.needs_session")
        self._session, self._session_wanted = outer_session, outer_wanted
        self._test_clients = outer_test_clients

    visit_AsyncFunctionDef = visit_FunctionDef

    def _ensure_request_param(
        self, node, keyword_only: bool = False, session: str | None = None
    ) -> None:
        """Give the handler a leading `request: Request`, as wreath calls it.

        `keyword_only` additionally writes a bare `*` after it, which makes
        every remaining parameter keyword-only. That is what lets a required
        parameter keep its required-ness: a plain parameter with no default
        cannot follow one with a default, but a keyword-only one can, and
        wreath hands every bound value over by name (`handler(request,
        **kwargs)`), so nothing about the call changes.

        `session` names a session parameter to add alongside it, for a handler
        whose body has queries to run. Wreath fills it in from the application's
        registry, so no caller changes.
        """
        args = node.args
        positional = list(args.posonlyargs) + list(args.args)
        # A signature that already has a `*` (or a `*args`) cannot be given a
        # second one, and does not need one — everything after it is already
        # keyword-only.
        star = "*, " if keyword_only and not (args.kwonlyargs or args.vararg) else ""
        extra = ""
        parameters = positional + list(args.kwonlyargs)
        reuses_session = any(
            arg.arg == session
            and arg.annotation is not None
            and self._is_orm_session(arg.annotation)
            for arg in parameters
        )
        if session == _SESSION_PARAM and not reuses_session:
            self.needs_annotated = True
            self.needs.update({"Session", "FromORM"})
            extra = f"{_SESSION_PARAM}: Annotated[Session, FromORM()], "
        # Any parameter already named `request` satisfies the injection. Checking
        # only position 0 produced `async def f(request: Request, x, request: Request)`
        # for a handler that declared `request` second — which `ast.parse` accepts
        # (duplicate arguments are a *compile* error, so the round-trip guard let
        # it through) and CPython then refuses to compile.
        existing = next((a for a in positional if a.arg == "request"), None)
        has_request = existing is not None or any(a.arg == "request" for a in args.kwonlyargs)
        if has_request:
            if not (star or extra):
                return
            if existing is positional[0] and len(positional) > 1:
                s = self.buf.start_of(positional[1])
                self.buf._edits.append((s, s, f"{star}{extra}".encode()))
            elif existing is not None:
                end = self.buf.end_of(existing)
                self.buf._edits.append((end, end, f", {star}{extra}".rstrip(" ,").encode()))
            else:
                self._note(
                    node.lineno,
                    "route.method",
                    "move `request` to the front, then add "
                    "`session: Annotated[Session, FromORM()]` after it so the "
                    "queries in this handler have a session to run through",
                )
            return
        self.needs.add("Request")
        if positional:
            first = positional[0]
            s = self.buf.start_of(first)
            self.buf._edits.append((s, s, f"request: Request, {star}{extra}".encode()))
            return
        open_paren = self.buf.b.find(b"(", self.buf.start_of_line(node.lineno))
        if open_paren == -1:
            self._note(
                node.lineno,
                "route.method",
                "add a `request: Request` parameter -- every wreath handler takes one first",
            )
            return
        if args.kwonlyargs or args.vararg or args.kwarg:
            self.buf._edits.append(
                (open_paren + 1, open_paren + 1, f"request: Request, {extra}".encode())
            )
            return
        close_paren = self.buf.b.find(b")", open_paren)
        if close_paren == -1 or self.buf.b[open_paren + 1 : close_paren].strip():
            self._note(
                node.lineno,
                "route.method",
                "add a `request: Request` parameter -- every wreath handler takes one first",
            )
            return
        tail = f", {star}{extra}".rstrip(" ,") if extra else ""
        self.buf._edits.append((open_paren + 1, close_paren, f"request: Request{tail}".encode()))

    def _route_needs_keyword_only(self, node) -> bool:
        """Whether porting this signature leaves a required parameter after a defaulted one."""
        args = node.args
        defaults = dict(
            zip(
                [a.arg for a in args.args[len(args.args) - len(args.defaults) :]],
                args.defaults,
                strict=True,
            )
        )
        defaulted = False
        for arg in args.args:
            if arg.arg == "request":
                continue
            default = defaults.get(arg.arg)
            required_marker = (
                isinstance(default, ast.Call)
                and arg.annotation is not None
                and self.imports.origin(default.func).split(".")[-1] in _MARKERS
                and _marker_default(default) is None
            )
            if default is not None and not required_marker:
                defaulted = True
            elif defaulted:
                return True
        return False

    def _rewrite_as_form_params(self, node) -> None:
        """`x: T = Depends(<Model>.as_form)` -> `x: Annotated[T, Form()]`.

        Whole-model Form binding.
        """
        args = node.args
        # Both pairings are equal-length by construction: the tail of `args.args`
        # is sliced to `len(args.defaults)`, and the AST keeps `kw_defaults` the
        # same length as `kwonlyargs`, padding with None.
        defaulted = list(
            zip(args.args[len(args.args) - len(args.defaults) :], args.defaults, strict=True)
        )
        defaulted += [
            (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None
        ]
        for arg, default in defaulted:
            if not (
                isinstance(default, ast.Call)
                and default.args
                and self.imports.origin(default.func).split(".")[-1] == "Depends"
            ):
                continue
            dep = default.args[0]
            if (
                isinstance(dep, ast.Attribute)
                and dep.attr == "as_form"
                and arg.annotation is not None
            ):
                self.needs_annotated = True
                self.needs.add("Form")  # -> wreath.binding via _WREATH_MODULE
                new = f"{arg.arg}: Annotated[{self._seg(arg.annotation)}, Form()]"
                s = self.buf.start_of(arg)
                e = self.buf.end_of(default)
                self.buf._edits.append((s, e, new.encode("utf-8")))

    def _rewrite_route_options(self, dec: ast.Call, node) -> None:
        """Translate `status_code=`/`response_model=`; annotate what can't be done safely."""
        drop: set[str] = set()
        sc = next((kw for kw in dec.keywords if kw.arg == "status_code"), None)
        if sc is not None:
            rule_id = status_code_rule(self.imports, sc.value, node)
            wrapper = _STATUS_WRAPPER.get(rule_id)
            returns = _returns_in(node)
            # `returned is not None` is implied by both wrapper verdicts (each
            # requires a single return *of a literal*). Checked rather than
            # asserted so an future rule added to `_STATUS_WRAPPER` without that
            # guarantee degrades to an annotation instead of a bad edit.
            returned = returns[0].value if len(returns) == 1 else None
            if wrapper is not None and returned is not None:
                status = status_int(self.imports, sc.value)
                self.needs.add(wrapper)
                self.buf.replace(returned, f"{wrapper}({self._seg(returned)}, status={status})")
                drop.add("status_code")
            else:
                self._annotate(dec.lineno, rule_id)
        if any(kw.arg == "response_model" for kw in dec.keywords):
            # translated: drop the kwarg (the return annotation is the schema source)
            drop.add("response_model")
        rc = next((kw for kw in dec.keywords if kw.arg == "response_class"), None)
        if rc is not None:
            # The same verdict the report gives, so the keyword is only deleted
            # where the report called that deletion a no-op. Anything else keeps
            # its kwarg *and* its note: dropping `response_class=HTMLResponse`
            # would change the content type of every response the route sends.
            if response_class_rule(self.imports, rc.value, node) == "route.response_class_default":
                drop.add("response_class")
                self._resolve(dec.lineno, "route.response_class_default")
        if any(kw.arg == "include_in_schema" and _is_false(kw.value) for kw in dec.keywords):
            self._resolve(dec.lineno, "route.include_in_schema")
        if drop:
            parts = [self._seg(a) for a in dec.args]
            parts += [
                (f"{kw.arg}={self._seg(kw.value)}" if kw.arg else f"**{self._seg(kw.value)}")
                for kw in dec.keywords
                if kw.arg not in drop
            ]
            self.buf.replace(dec, f"{self._seg(dec.func)}({', '.join(parts)})")

    def _split_markers(self, node) -> None:
        args = node.args
        defaulted = args.args[len(args.args) - len(args.defaults) :]
        for arg, default in zip(defaulted, args.defaults, strict=True):
            if not isinstance(default, ast.Call):
                continue
            marker = self.imports.origin(default.func).split(".")[-1]
            if marker not in _MARKERS or arg.annotation is None:
                continue
            self._rewrite_marker_param(arg, default, marker)

    def _rewrite_marker_param(self, arg, call: ast.Call, marker: str) -> None:
        ann = self._seg(arg.annotation)
        default = _marker_default(call)
        default_src = None if default is None else self._seg(default)
        # A required marker becomes a parameter with no default at all. That used
        # to be left alone, because such a parameter cannot follow a defaulted
        # one — but `_ensure_request_param` has already put a `*` in front of the
        # bound parameters when this signature needed one, and keyword-only
        # parameters may be declared in any order.
        kept, dropped = [], []
        for kw in call.keywords:
            if kw.arg == "default" or kw.arg in _MARKER_DOC_KWARGS:
                continue  # already read, or documentation only
            if kw.arg in _KW_RENAME:
                kept.append(f"{_KW_RENAME[kw.arg]}={self._seg(kw.value)}")
            elif kw.arg in _KW_KEEP:
                kept.append(f"{kw.arg}={self._seg(kw.value)}")
            else:
                dropped.append(kw.arg)
        self.needs_annotated = True
        marker_call = f"{marker}({', '.join(kept)})"
        new = f"{arg.arg}: Annotated[{ann}, {marker_call}]"
        if default_src is not None:
            new += f" = {default_src}"
        # replace the whole "arg: T = Marker(...)" span
        s = self.buf.start_of(arg)
        e = self.buf.end_of(call)
        self.buf._edits.append((s, e, new.encode("utf-8")))
        if dropped:
            self._annotate(
                arg.lineno,
                "param.query_strconstraint",
                "dropped from the marker: " + ", ".join(f"{n}=" for n in dropped),
            )
