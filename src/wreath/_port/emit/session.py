"""A query that runs needs a session, and a session comes from the caller.

Which handlers get one, under what name, and how it reaches the call. It
reads `.queries` because whether a function needs a session at all is a
question about the chains inside it."""

from __future__ import annotations

import ast

from ..foreign import is_atomic_block
from .buffer import _ends_argument_list
from .queries import _QueryRewrite
from .targets import _SESSION_PARAM


class _SessionThreading(_QueryRewrite):
    def visit_With(self, node: ast.With) -> None:
        """`with transaction.atomic():` -> `async with session.begin():`.

        Both halves move: the context manager is asynchronous, so the `with`
        becomes an `async with`, and the block belongs to a session rather than
        to a thread-local Django connection. Nesting needs no special case --
        wreath opens a savepoint when a transaction is already open, which is
        what Django's nested `atomic()` does too.

        Rewritten only where a session is already in scope, exactly as a running
        query is: adding one changes the signature and every caller, and that is
        the decision `orm.query.needs_session` hands back rather than makes.
        """
        for item in node.items:
            call = item.context_expr
            if not (
                isinstance(call, ast.Call)
                and is_atomic_block(call, self.imports)
                and item.optional_vars is None
                and len(node.items) == 1
            ):
                continue
            if self._session is None:
                self._session_wanted = True
                self._annotate(call.lineno, "orm.transaction.atomic")
                break
            start = self.buf.start_of_line(node.lineno)
            indent = self.buf.b[start:].index(b"with") + start
            self.buf._edits.append((indent, indent, b"async "))
            self._replace_all_of(call, f"{self._session}.begin()")
            self._resolve(call.lineno, "orm.transaction.atomic")
            break
        self.generic_visit(node)

    def _session_definition_selected(self, node) -> bool:
        if self.session_sites_resolved:
            return (self._source_path, node.lineno) in self.session_definition_sites
        return node.name in self.session_functions

    def _session_call_selected(self, node: ast.Call, called: str | None) -> bool:
        if self.session_sites_resolved:
            return (
                self._source_path,
                node.lineno,
                node.col_offset,
            ) in self.session_call_sites
        return called in self.session_functions

    def _route_session_name(self, node) -> str | None:
        """The name a session will be reachable under inside this handler.

        A handler that already takes a wreath session keeps its own name.
        Otherwise wreath can supply one — but only if the name is going spare,
        and only if the body has a query that needs it.

        "Already takes a session" is decided by resolving the annotation, not by
        looking for the word. `session: Session` is just as likely to be a
        pydantic model of an incoming payload as a database handle, and reading it
        the wrong way produced a handler with the parameter declared twice.
        """
        parameters = list(node.args.args) + list(node.args.kwonlyargs)
        for arg in parameters:
            if arg.annotation is not None and self._is_orm_session(arg.annotation):
                return arg.arg
        if any(arg.arg == _SESSION_PARAM for arg in parameters):
            return None  # the name is taken by something else
        if not (self._name_is_free("Session") and self._name_is_free("FromORM")):
            return None  # so is the type's name
        if self._session_definition_selected(node) or self._runs_a_query(node):
            return _SESSION_PARAM
        if self.opinionated and self._calls_a_session_function(node):
            return _SESSION_PARAM
        return None

    def _calls_a_session_function(self, node) -> bool:
        """Whether this body calls something that now needs a session passed in."""
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if self._session_call_selected(
                inner,
                inner.func.attr
                if isinstance(inner.func, ast.Attribute)
                else getattr(inner.func, "id", None),
            ):
                return True
            name = (
                inner.func.attr
                if isinstance(inner.func, ast.Attribute)
                else getattr(inner.func, "id", None)
            )
            if name in self.session_functions:
                return True
        return False

    def _can_take_session(self, node) -> bool:
        """Whether `session: Session` can be added to this signature safely."""
        taken = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        return _SESSION_PARAM not in taken and self._name_is_free("Session")

    def _pass_session(self, node: ast.Call) -> None:
        """Add `session=session` to a call whose target now takes one.

        Written in just before the call's closing parenthesis, not after the last
        argument: an argument's own span stops before any brackets wrapped around
        it, so appending there landed the keyword *inside* a parenthesised
        expression.
        """
        close = self.buf.end_of(node) - 1
        if self.buf.b[close : close + 1] != b")":
            return  # not a plain call span; leave it alone
        # `sum(x for x in xs)` may write its generator bare only while it is the
        # sole argument. Adding a second one means adding its brackets too.
        if len(node.args) == 1 and isinstance(node.args[0], ast.GeneratorExp) and not node.keywords:
            start, end = self.buf.start_of(node.args[0]), self.buf.end_of(node.args[0])
            self.buf._edits.append((start, start, b"("))
            self.buf._edits.append((end, end, b")"))
        separator = "" if _ends_argument_list(self.buf.b, close) else ", "
        self.buf._edits.append(
            (close, close, f"{separator}{_SESSION_PARAM}={self._session}".encode())
        )

    def _add_session_param(self, node) -> None:
        """Add a keyword-only `session: Session` to an ordinary function.

        Keyword-only wherever it can be, so it never has to be threaded through
        positional call sites and never collides with an existing default.
        """
        self.needs.add("Session")
        args = node.args
        if args.kwonlyargs:
            last = args.kwonlyargs[-1]
            end = self.buf.end_of(args.kw_defaults[-1] or last)
            text = f", {_SESSION_PARAM}: Session"
        elif args.vararg is not None:
            end = self.buf.end_of(args.vararg)
            text = f", {_SESSION_PARAM}: Session"
        elif args.args or args.posonlyargs:
            positional = list(args.posonlyargs) + list(args.args)
            last = positional[-1]
            default = args.defaults[-1] if args.defaults else None
            end = self.buf.end_of(default if default is not None else last)
            text = f", *, {_SESSION_PARAM}: Session"
        else:
            open_paren = self.buf.b.find(b"(", self.buf.start_of_line(node.lineno))
            if open_paren == -1:
                return
            end = open_paren + 1
            text = f"{_SESSION_PARAM}: Session"
        self.buf._edits.append((end, end, text.encode()))

    def _is_orm_session(self, annotation: ast.expr) -> bool:
        """Whether this annotation is wreath's `Session`, however it is wrapped."""
        if isinstance(annotation, ast.Subscript):  # Annotated[Session, FromORM()]
            inner = annotation.slice
            annotation = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return self.imports.origin(annotation).startswith("wreath.orm")

    def _name_is_free(self, name: str) -> bool:
        """Whether the module can be handed `name` without shadowing its own.

        Every injected import is a new global, and a module that already binds
        that name to something of its own would silently get the wrong one.
        """
        bound = self.imports.names.get(name)
        return bound is None or bound.startswith("wreath")
