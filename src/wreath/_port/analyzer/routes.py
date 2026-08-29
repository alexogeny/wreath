"""Route handlers: the HTTP verbs, the parameter markers a signature carries,
and the lifespan shapes an application hands to `lifespan=`."""

from __future__ import annotations

import ast

from .imports import _Imports

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_MARKER_RULE = {
    "Query": "param.query",
    "Path": "param.path",
    "Header": "param.header",
    "Cookie": "param.cookie",
    "Form": "param.form",
    "File": "param.file",
}


def _is_lifespan(node, lifespan_names, imports: _Imports) -> bool:
    """Whether this function is *the app's* lifespan, and not merely an
    `@asynccontextmanager`.

    `contextlib.asynccontextmanager` is stdlib, and an advisory-lock or
    connection helper written with it needs no porting at all. Recognized three
    ways: registered as a lifespan on the app, named `lifespan` by convention,
    or taking exactly one `FastAPI`/`Starlette`-annotated parameter.
    """
    if node.name in lifespan_names or node.name == "lifespan":
        return True
    parameters = list(node.args.args) + list(node.args.posonlyargs)
    return (
        len(parameters) == 1
        and parameters[0].annotation is not None
        and imports.origin(parameters[0].annotation).split(".")[-1] in ("FastAPI", "Starlette")
    )


def lifespan_names(tree: ast.Module) -> frozenset[str]:
    """Names handed to an application as `lifespan=<name>` anywhere in the module."""
    return frozenset(
        keyword.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "lifespan" and isinstance(keyword.value, ast.Name)
    )


def lifespan_shape(node) -> tuple[str, str]:
    """`(rule_id, reason)` for one lifespan body.

    The split into `@app.on_startup`/`@app.on_shutdown` is determined exactly
    when the body *is* a split: one bare `yield` as a top-level statement, with
    the halves independent. Three things break that, and each is worth naming
    rather than lumping together, because they need different fixes:

    * the yield hands a value to the framework (FastAPI's lifespan-state dict),
      which has to find a home on `app.state`;
    * the yield sits inside a `try`/`async with`, so the shutdown half is
      that block's exit rather than a suffix of the body;
    * a name made before the yield is used after it — the halves are separate
      functions, so that name needs somewhere to live.
    """
    yields = [
        (index, statement)
        for index, statement in enumerate(node.body)
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield)
    ]
    if len(yields) != 1:
        nested = any(isinstance(n, ast.Yield) for n in ast.walk(node))
        return "lifespan.ctx", (
            "the yield is inside a try/async with, so the shutdown half is that block's exit"
            if nested
            else "no top-level yield to split at"
        )
    index, statement = yields[0]
    if statement.value.value is not None:  # type: ignore[union-attr]
        return "lifespan.ctx", "the yield hands a value to the framework; put it on app.state"
    crossing = _names_crossing(node.body[:index], node.body[index + 1 :])
    if crossing:
        return "lifespan.ctx", (
            "startup makes " + ", ".join(crossing) + " and shutdown uses them, so they need a "
            "home the two hooks share (app.state)"
        )
    return "lifespan.split", ""


def _names_crossing(before: list[ast.stmt], after: list[ast.stmt]) -> list[str]:
    """Names bound in `before` and read in `after`, in binding order."""
    bound: list[str] = []
    seen: set[str] = set()
    for statement in before:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                if node.id not in seen:
                    seen.add(node.id)
                    bound.append(node.id)
    read = {
        node.id
        for statement in after
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return [name for name in bound if name in read]
