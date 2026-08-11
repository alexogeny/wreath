"""Foreign-framework constructs rewritten in place.

The constructs five frameworks spell differently and mean identically. The table
that decides what each becomes is `.._port.frameworks`, shared with the report so
a line the report calls translated is one this writes out.

Two edits, both local, both at a `raise`/`return` site:

* an HTTP error becomes `raise <Class>()` from `wreath.exceptions` -- there is no
  `abort()` and no status argument, because the status is a class attribute; and
* a redirect becomes `return RedirectResponse(url, status=...)`, always with the
  status, because wreath's default is 307 and none of these are.

The second is the one with a trap in it. Flask's and Bottle's `redirect()`
*returns* a response and Pyramid's `HTTPFound` is *raised*; wreath's is returned
either way, so a raised one has its `raise` replaced by a `return` rather than
just its call rewritten.
"""

from __future__ import annotations

import ast

from ..foreign import (
    _callee,
    _redirect_status,
    blueprint_router,
    redirect_target,
    route_pattern,
    route_translates,
)
from ..frameworks import raised_exception, route_methods, wreath_path
from .state import _EmitterState

#: Root packages whose HTTP vocabulary these rules translate.
_WEB_ROOTS = frozenset({"flask", "aiohttp", "tornado", "pyramid", "bottle", "django"})

#: Runtimes whose presence refuses the whole tree. A rewrite under a monkeypatch
#: produces code that passes its tests at low concurrency and serialises in
#: production, so a Bottle app that *looks* ported is worse than one that does
#: not: the same gate the report applies, applied to the file.
_PATCHED_ROOTS = frozenset({"gevent", "eventlet"})


class _ForeignRewrite(_EmitterState):
    def _foreign_call(self, node: ast.Call) -> bool:
        """Rewrite one foreign HTTP construct, if this call is one.

        Gated exactly as the report is -- a module that speaks one of these
        frameworks, and no monkeypatch anywhere in it -- because a verdict and a
        rewrite that disagree about a line is the failure this whole package is
        arranged to prevent.
        """
        roots = self.imports.roots
        if not (roots & _WEB_ROOTS) or (roots & _PATCHED_ROOTS):
            return False
        if self._inside_replaced(node):
            return False
        name = _callee(node)
        if name in ("Flask", "Bottle"):
            # `Flask(__name__)` passes the import name so Flask can find
            # templates and static files relative to the module. Wreath locates
            # neither that way -- `app.static(prefix, directory)` is explicit --
            # so the argument has nowhere to go and needs none.
            self.needs.add("Wreath")
            self._replace_all_of(node, "Wreath()")
            self._resolve(node.lineno, "port.app.wreath")
            return True
        if name == "RouteTableDef":
            self.needs.add("Router")
            self._replace_all_of(node, "Router()")
            self._resolve(node.lineno, "port.router.new")
            return True
        if name == "Blueprint":
            return self._rewrite_blueprint(node)
        if name in ("register_blueprint", "add_routes") and isinstance(
            node.func, ast.Attribute
        ):
            owner = self._seg(node.func.value)
            if len(node.args) != 1 or node.keywords:
                return False
            self._replace_all_of(
                node, f"{owner}.include_router({self._seg(node.args[0])})"
            )
            self._resolve(node.lineno, "port.router.include")
            return True
        status = _redirect_status(name, node)
        if status is not None:
            return self._rewrite_http_redirect(node, status)
        if raised_exception(name, node) is not None:
            return self._rewrite_http_error(node)
        return False

    def _rewrite_blueprint(self, node: ast.Call) -> bool:
        """`Blueprint("plots", __name__, url_prefix="/plots")` -> `Router(...)`.

        The blueprint's name becomes the router's `tags`, which is where wreath
        puts the grouping the name was doing -- it is what the generated schema
        groups by, and it is what `url_for("plots.read_plot")` was reading.
        """
        found = blueprint_router(node)
        if found is None:
            return False
        name, prefix = found
        parts = []
        if prefix is not None:
            parts.append(f"prefix={prefix!r}")
        parts.append(f"tags=({name!r},)")
        self.needs.add("Router")
        self._replace_all_of(node, f"Router({', '.join(parts)})")
        self._resolve(node.lineno, "port.router.new")
        return True

    def _foreign_route(self, node, dec: ast.expr, attr: str) -> bool:
        """`@app.route("/p/<int:id>", methods=["POST"])` -> `@app.post("/p/{id}")`.

        Returns whether the decorator and the signature were rewritten; the
        caller adds the leading `request: Request`, because that is its job for
        every route and this one is not special.

        The handler keeps its `def`. Wreath dispatches a synchronous handler
        natively -- it even has a fast path for an application whose handlers are
        all sync -- so there is nothing to gain by making a WSGI handler `async`,
        and a great deal to lose: its body is full of blocking calls that were
        fine on a worker thread and are not fine on an event loop.
        """
        framework = self._foreign_framework()
        if framework is None or not isinstance(dec, ast.Call):
            return False
        if not route_translates(dec, attr, framework, node):
            return False
        pattern = route_pattern(dec)
        methods = route_methods(attr, dec)
        converted = wreath_path(pattern or "", framework)
        if pattern is None or methods is None or converted is None:
            return False
        new_pattern, annotations = converted
        owner = self._seg(dec.func.value) if isinstance(dec.func, ast.Attribute) else ""
        kept = [
            f"{keyword.arg}={self._seg(keyword.value)}"
            for keyword in dec.keywords
            if keyword.arg in ("name", "endpoint")
        ]
        # `endpoint=` is Flask's name for the same thing wreath calls `name=`,
        # and it is what `url_for` looks the route up by.
        kept = [part.replace("endpoint=", "name=", 1) for part in kept]
        if len(methods) == 1:
            verb, extra = methods[0], ""
        else:
            # More than one method on one function is `route(..., methods=(...))`
            # in wreath too, so the shape survives rather than being split.
            verb = "route"
            extra = ", methods=(" + ", ".join(f'"{m.upper()}"' for m in methods) + ",)"
        arguments = ", ".join([f'"{new_pattern}"{extra}', *kept])
        self._replace_all_of(dec, f"{owner}.{verb}({arguments})")
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            annotation = annotations.get(argument.arg)
            if annotation is None or argument.annotation is not None:
                continue
            end = self.buf.end_of(argument)
            self.buf._edits.append((end, end, f": {annotation}".encode()))
        self._resolve(getattr(dec, "lineno", node.lineno), "port.route.method")
        return True

    def _foreign_framework(self) -> str | None:
        """Which of the five this module speaks, or `None` if it is none of them.

        Order matters only where two are imported at once, which is a porting
        tree mid-flight; the route spellings are disjoint enough that the first
        match is the right one.
        """
        roots = self.imports.roots
        if roots & _PATCHED_ROOTS:
            return None
        for framework in ("flask", "bottle", "aiohttp", "django", "pyramid", "tornado"):
            if framework in roots:
                return framework
        return None

    def _rewrite_http_error(self, node: ast.Call) -> bool:
        """`abort(404)` / `web.HTTPNotFound(reason=x)` -> `raise NotFound(x)`.

        Returns whether it happened. The `raise` is supplied where the source
        did not have one: Flask's `abort()` and Bottle's are ordinary calls that
        raise internally, so replacing the call alone would leave a statement
        that builds an exception and discards it -- a route that answered 404
        before the port and falls through to the handler's own return after it.
        """
        found = raised_exception(_callee(node), node)
        if found is None:
            return False
        wreath_class, detail = found
        self.needs.add(wreath_class)
        argument = "" if detail is None else self._seg(detail)
        text = f"{wreath_class}({argument})"
        parent = self._parents.get(id(node))
        if isinstance(parent, ast.Raise):
            self._replace_all_of(node, text)
        elif isinstance(parent, ast.Expr):
            # A bare `abort(404)` statement. It raises in Flask and Bottle, and
            # nothing about the call site says so.
            self._replace_all_of(parent, f"raise {text}")
        elif isinstance(parent, ast.Return):
            self._replace_all_of(parent, f"raise {text}")
        else:
            return False
        self._resolve(node.lineno, "port.http.exception")
        return True

    def _rewrite_http_redirect(self, node: ast.Call, status: int) -> bool:
        """`redirect(url, 301)` / `HTTPFound(location=url)` -> `RedirectResponse`.

        The status is written out every time. Wreath's `RedirectResponse`
        defaults to 307 and every foreign spelling here is 301/302/303, so an
        omitted status is not "nothing to carry" -- it is a permanent redirect
        quietly becoming a temporary one.
        """
        target = redirect_target(_callee(node), node)
        if target is None:
            return False
        self.needs.add("RedirectResponse")
        text = f"RedirectResponse({self._seg(target)}, status={status})"
        parent = self._parents.get(id(node))
        if isinstance(parent, ast.Raise):
            # Pyramid raises its redirect; wreath returns one.
            self._replace_all_of(parent, f"return {text}")
        elif isinstance(parent, ast.Expr):
            # Bottle's `redirect(...)` raises internally, so a bare statement is
            # the end of the handler however it reads.
            self._replace_all_of(parent, f"return {text}")
        else:
            self._replace_all_of(node, text)
        self._resolve(node.lineno, "port.http.redirect")
        return True
