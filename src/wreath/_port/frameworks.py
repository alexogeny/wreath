"""What one foreign-framework construct becomes in wreath.

The tables, and nothing else. `.foreign` reads them to decide a verdict and
`.emit.frameworks` reads them to write the replacement, so a construct the
report calls translated is one the emitter has an entry for -- the same reason
`query_rule()` is shared rather than copied.

Five frameworks converge here because they say the same things. An HTTP error is
`abort(404)` in Flask and Bottle, `web.HTTPNotFound()` in aiohttp,
`HTTPNotFound()` in Pyramid, `HTTPError(404)` in Tornado and `Http404()` in
Django, and every one of them is `raise NotFound()`. Keeping one table of the
answers rather than one per framework is what stops the fifth spelling being the
one nobody remembered.

**A status with no wreath class is not translated.** `wreath.exceptions` ships
400/401/403/404/405/409/413/422/429/431/500 and a 418 has no home, so it stays
refused by name rather than being rounded to the nearest class that exists.
"""

from __future__ import annotations

import ast

from .analyzer.responses import STATUS_EXCEPTION

#: Foreign exception class -> the HTTP status it carries. aiohttp spells these
#: `web.HTTPNotFound` and Pyramid `pyramid.httpexceptions.HTTPNotFound`; the
#: trailing name is the same, which is why resolution is by tail.
#:
#: Redirects are deliberately absent: `HTTPFound` is a *response* in Pyramid and
#: raising it is how Pyramid returns one. Wreath returns `RedirectResponse`, so
#: that is a return-site rewrite and lives in `REDIRECT_STATUS` below.
FOREIGN_EXCEPTION_STATUS: dict[str, int] = {
    "HTTPBadRequest": 400,
    "HTTPUnauthorized": 401,
    "HTTPForbidden": 403,
    "HTTPNotFound": 404,
    "HTTPMethodNotAllowed": 405,
    "HTTPConflict": 409,
    "HTTPRequestEntityTooLarge": 413,
    "HTTPUnprocessableEntity": 422,
    "HTTPTooManyRequests": 429,
    "HTTPInternalServerError": 500,
    # Django's one-off. `Http404("...")` is the whole of Django's HTTP-exception
    # vocabulary that a view raises directly.
    "Http404": 404,
}

#: Foreign redirect response -> the status it means. Wreath's `RedirectResponse`
#: defaults to **307** and every one of these is 301/302, so the status is never
#: droppable: leaving it off silently changes a permanent redirect into a
#: method-preserving temporary one, and a GET-after-POST into a re-POST.
REDIRECT_STATUS: dict[str, int] = {
    "HTTPMovedPermanently": 301,
    "HTTPFound": 302,
    "HTTPSeeOther": 303,
    "HTTPTemporaryRedirect": 307,
    "HTTPPermanentRedirect": 308,
    "HttpResponsePermanentRedirect": 301,
    "HttpResponseRedirect": 302,
}


#: Path converter -> the Python annotation wreath binds it with
#: (`binding._convert_scalar`). `path` is the one converter wreath spells in the
#: pattern itself, as a trailing `{name:path}` (`_routing.py:64`); the rest are a
#: bare `{name}` plus this annotation.
#:
#: `uuid` is deliberately absent and is the commonest refusal here.
#: `binding._convert_scalar` converts `str`, `int`, `float`, `bool`, `Instant`,
#: `datetime` and `date`; a `uuid.UUID` annotation raises "unsupported parameter
#: annotation", so Flask's, Django's and aiohttp's UUID captures all stay
#: refused rather than being downgraded to `str` -- which would widen the route
#: to match anything.
PATH_CONVERTER: dict[str, str] = {
    "": "str",
    "str": "str",
    "string": "str",
    "slug": "str",
    "int": "int",
    "float": "float",
    "path": "str",
}

#: Where the converter sits relative to the name. Flask and Django write
#: `<int:plot_id>`; Bottle writes `<sailing_id:int>`. Same brackets, opposite
#: order, and reading one as the other silently produces a parameter named after
#: a type.
_CONVERTER_FIRST = frozenset({"flask", "django"})


def wreath_path(pattern: str, framework: str) -> tuple[str, dict[str, str]] | None:
    """`("/plots/{plot_id}", {"plot_id": "int"})` for one foreign URL pattern.

    `None` when any placeholder has no wreath form. Refusing the whole pattern
    rather than the one placeholder is deliberate: a half-translated path is a
    route that answers on a URL nobody wrote.

    A regex converter (`<code:re:[A-Z]{2}>`, `{year:\\d{4}}`) is the sharp case.
    Wreath's only converter is `path`, so downgrading `<code:re:...>` to
    `{code}` widens the route to match any single segment -- a behaviour change
    dressed as a port, and one that shows up as a 200 where there was a 404.
    """
    out: list[str] = []
    annotations: dict[str, str] = {}
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "{":
            # Already wreath's spelling (aiohttp, Pyramid). A converter inside it
            # is a regex, which has no wreath form.
            end = pattern.find("}", index)
            if end < 0:
                return None
            inner = pattern[index + 1 : end]
            if ":" in inner:
                name, _, converter = inner.partition(":")
                if converter != "path":
                    return None
                out.append(f"{{{name}:path}}")
            else:
                out.append(f"{{{inner}}}")
                annotations[inner] = "str"
            index = end + 1
            continue
        if character != "<":
            out.append(character)
            index += 1
            continue
        end = pattern.find(">", index)
        if end < 0:
            return None
        inner = pattern[index + 1 : end]
        if framework in _CONVERTER_FIRST:
            converter, separator, name = inner.partition(":")
            if not separator:
                converter, name = "", inner
        else:
            name, separator, converter = inner.partition(":")
            if not separator:
                converter = ""
        if not name or ":" in converter or not name.isidentifier():
            return None  # a regex converter, or something this cannot read
        annotation = PATH_CONVERTER.get(converter)
        if annotation is None:
            return None
        out.append(f"{{{name}:path}}" if converter == "path" else f"{{{name}}}")
        annotations[name] = annotation
        index = end + 1
    return "".join(out), annotations


#: `abort(...)` in Flask and Bottle, and Tornado's `HTTPError(...)`: the status
#: is the first positional argument rather than the class name.
_STATUS_FIRST = frozenset({"abort", "HTTPError"})


#: The wreath verb decorators, so a route naming exactly one method becomes it.
_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def route_methods(attr: str, call: ast.Call | None) -> tuple[str, ...] | None:
    """The HTTP methods one foreign route decorator answers, or `None`.

    Flask's `@app.route(p)` defaults to **GET and HEAD**, and wreath's `get`
    answers HEAD too, so the default is `("get",)` rather than a loss. Bottle
    spells the same thing `method="DELETE"`, aiohttp and Flask both spell the
    verb decorators the way wreath does, and a `methods=` list that is not
    literal is not readable at all.
    """
    if attr in _VERBS:
        return (attr,)
    if attr != "route":
        return None
    if call is None:
        return ("get",)
    named = next(
        (
            keyword.value
            for keyword in call.keywords
            if keyword.arg in ("methods", "method")
        ),
        None,
    )
    if named is None:
        return ("get",)
    values = (
        named.elts if isinstance(named, (ast.List, ast.Tuple, ast.Set)) else (named,)
    )
    methods: list[str] = []
    for value in values:
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            return None
        method = value.value.lower()
        if method == "head" and "get" in methods:
            continue  # a wreath GET route answers HEAD already
        if method not in _VERBS:
            return None
        methods.append(method)
    return tuple(methods) or ("get",)


def status_argument(call: ast.Call) -> int | None:
    """The literal status `abort(404)` / `HTTPError(404, ...)` names, if any."""
    argument = call.args[0] if call.args else None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, int):
        return None if isinstance(argument.value, bool) else argument.value
    return None


def raised_exception(name: str, call: ast.Call) -> tuple[str, ast.expr | None] | None:
    """`(wreath class, detail expression)` for one foreign HTTP error, or `None`.

    `None` means it is not one, or that wreath ships no class for the status --
    a 418 has no home in `wreath.exceptions` and is refused by name rather than
    rounded to whichever class happens to exist.

    The detail is where the frameworks disagree and it matters. Flask spells it
    `description=`, aiohttp and Tornado spell it `reason=`, and Pyramid and
    Django pass it positionally. **Tornado's second positional is not the
    detail** -- `HTTPError(status, log_message)` writes that one to the log and
    never to the client, so carrying it across would publish an internal message
    to every caller. Only `reason=` is the detail there.
    """
    if name in _STATUS_FIRST:
        status = status_argument(call)
        if status is None:
            return None
        wreath_class = exception_class_for_status(status)
        if wreath_class is None:
            return None
        detail = next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg in ("description", "reason")
            ),
            None,
        )
        if detail is None and name == "abort" and len(call.args) > 1:
            # Bottle's `abort(409, "text")`. Tornado's second positional is
            # deliberately not read here -- it is a log line, not a reason.
            detail = call.args[1]
        return (wreath_class, detail)
    wreath_class = exception_class(name)
    if wreath_class is None:
        return None
    detail = next(
        (
            keyword.value
            for keyword in call.keywords
            if keyword.arg in ("reason", "detail", "explanation")
        ),
        None,
    )
    if detail is None and call.args:
        detail = call.args[0]
    return (wreath_class, detail)


def exception_class(name: str) -> str | None:
    """The `wreath.exceptions` class one foreign exception name becomes."""
    status = FOREIGN_EXCEPTION_STATUS.get(name)
    return None if status is None else STATUS_EXCEPTION.get(status)


def exception_class_for_status(status: int) -> str | None:
    """The `wreath.exceptions` class for a literal status, or `None` for a gap.

    `abort(404)` and `HTTPError(409, ...)` name the status rather than a class,
    which is the same question one step earlier.
    """
    return STATUS_EXCEPTION.get(status)
