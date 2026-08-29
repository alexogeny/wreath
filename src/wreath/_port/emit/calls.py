"""Calls rewritten in place: exceptions, caches, responses, and middleware
registration."""

from __future__ import annotations

import ast

from ..analyzer import (
    STATUS_EXCEPTION,
    celery_enqueue_rule,
    http_exception_rule,
    http_exception_status,
    pydantic_projection_rule,
    status_int,
)
from .frameworks import _ForeignRewrite
from .http_client import _HttpxRewrite
from .jobs import _BackgroundWork
from .models import _ModelRewrite
from .session import _SessionThreading
from .targets import (
    _ARROW_RENAME,
    _CACHE_RENAME,
    _RENAMED_ORIGINS,
    _RESPONSE_BODY_ARG,
    _SESSION_PARAM,
    _TEST_REQUEST_METHODS,
)
from .testing import _TestClient


# `_BackgroundWork` is a base rather than a sibling because `visit_Call` is
# defined here and dispatches into it: a second `visit_Call` in `jobs.py` is
# exactly the silent override this package layout exists to prevent.
class _CallRewrite(
    _ModelRewrite,
    _SessionThreading,
    _TestClient,
    _HttpxRewrite,
    _BackgroundWork,
    _ForeignRewrite,
):
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        origin = self.imports.origin(func)
        tail = origin.split(".")[-1]
        # `FastAPI`/`APIRouter` are renamed by `visit_Name`/`visit_Attribute`,
        # which reach every mention — an `app: FastAPI` annotation, a
        # `-> FastAPI` return, an `isinstance` check — and not only the call
        # that constructs the application.
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if (
            self.opinionated
            and isinstance(node, ast.Call)
            and self._asgi_test_client_app(node) is not None
            and not self._inside_replaced(node)
        ):
            self._replace_all_of(node, self._test_client_text(node))
        elif self.opinionated and origin == "httpx.AsyncClient":
            self._rewrite_httpx_client(node)
        elif self.opinionated and origin == "httpx.Timeout":
            total = self._http_timeout_total(node)
            if total is not None and not self._inside_replaced(node):
                self.needs.add("ClientTimeout")
                self._replace_all_of(node, f"ClientTimeout(total={total})")
                self._resolve(node.lineno, "ext.httpx")
        elif (
            self.opinionated
            and isinstance(func, ast.Attribute)
            and id(node) in self._http_requests
            and func.attr in _TEST_REQUEST_METHODS
        ):
            self._rewrite_httpx_request(node, self._http_requests[id(node)], func.attr)
        elif (
            self.opinionated
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (
                self._enclosing_callable_id(node),
                func.value.id,
            )
            in self._http_responses
            and func.attr == "json"
            and not node.args
            and not node.keywords
        ):
            self.needs.add("loads")
            self._replace_all_of(node, f"loads({func.value.id}.body)")
        if (
            self.opinionated
            and isinstance(func, ast.Name)
            and called in self.settings_models
            and called not in self._settings_custom_init
            and not node.args
            and not node.keywords
            and not self._inside_replaced(node)
        ):
            env_file, prefix = self._settings_bindings.get(called, (None, None))
            self.needs.add("Environment")
            if env_file is None:
                self.needs.add("read_osenv")
                environment = "Environment(read_osenv())"
            else:
                environment = f"Environment.load({env_file})"
            options = "" if prefix is None else f", prefix={prefix}"
            self._replace_all_of(
                node,
                f"{environment}.bind({called}{options})",
            )
        orm_query_call = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "objects"
        )
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in self._test_clients
            and func.attr in _TEST_REQUEST_METHODS
            and not isinstance(self._parents.get(id(node)), ast.Await)
        ):
            parent = self._parents.get(id(node))
            if isinstance(parent, (ast.Attribute, ast.Subscript)):
                # Attribute and subscription bind before ``await``. Merely
                # prefixing ``client.get(...).status`` produced an await of the
                # response attribute on the still-unawaited coroutine.
                self.buf._edits.append(
                    (self.buf.start_of(node), self.buf.start_of(node), b"(await ")
                )
                self.buf._edits.append((self.buf.end_of(node), self.buf.end_of(node), b")"))
            else:
                self.buf._edits.append(
                    (self.buf.start_of(node), self.buf.start_of(node), b"await ")
                )
        if (
            self.opinionated
            and self._session_call_selected(node, called)
            and self._session is not None
            and not orm_query_call
            and not any(kw.arg == _SESSION_PARAM for kw in node.keywords)
        ):
            # The callee gained a session parameter, so this call has to pass
            # one. Doing the signature and leaving the call is the half-port
            # that fails on its first request; `--opinionated` means both ends.
            # A ``Model.objects.create(...)`` call is not a call to a local
            # function named ``create``. The query rewriter below owns its
            # complete span and turns it into ``session.create(Model, ...)``;
            # inserting a keyword here overlaps that replacement and used to
            # leave the old manager call behind in the emitted tree.
            self._pass_session(node)
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("delay", "apply_async")
            and celery_enqueue_rule(node, inside_async=self._enclosing_is_async(node))
            == "bg.celery.enqueue"
        ):
            self._rewrite_celery_enqueue(node)
        if self._foreign_call(node):
            self.generic_visit(node)
            return
        if tail == "HTTPException":
            self._rewrite_http_exception(node)
        elif origin == "fastapi.encoders.jsonable_encoder" and len(node.args) == 1:
            # Wreath's JSON codec already serializes dataclasses, ORM rows,
            # UUIDs and datetimes, so the wrapper is the whole change: it goes,
            # and the value it wrapped stays.
            self._rewritten.add(id(func))
            self._replace_all_of(node, self._seg(node.args[0]))
        elif origin in _RENAMED_ORIGINS and _RENAMED_ORIGINS[origin] in _RESPONSE_BODY_ARG:
            self._rewrite_response_call(node, _RENAMED_ORIGINS[origin])
        elif origin.startswith("arrow.") and tail in _ARROW_RENAME:
            # `arrow.utcnow()` is `temporal.now()`. An `Instant` is a datetime
            # subclass, so it stores and serializes without a conversion at the
            # edges, and it refuses to be naive — which is the bug arrow's
            # implicit UTC hides.
            self.needs_temporal = True
            self._rewritten.add(id(func))
            self._replace_all_of(func, f"temporal.{_ARROW_RENAME[tail]}")
        elif origin.startswith("cachetools.") and tail in _CACHE_RENAME:
            self._rewrite_cache_call(node, func)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "get_pydantic"
            and pydantic_projection_rule(func, self._parents) == "pydantic.get_pydantic_exact"
        ):
            self._rewrite_model_dataclass(node, func)
        elif isinstance(func, ast.Attribute) and func.attr == "add_middleware":
            self._rewrite_add_middleware(node)
        # Everything else a call can be — a Celery `.delay()`, an `asyncio`
        # background loop, a boto3 client, a JWT decode, an Alembic cast — is
        # recognized by the analyzer and annotated from its findings (see
        # `annotate_findings`). Restating those tests here is how the emitter
        # ended up reporting *every* boto3 call as "keep the library" while the
        # report had already learned to route S3 at `wreath.objects`.
        self.generic_visit(node)

    def _rewrite_http_exception(self, node: ast.Call) -> None:
        """`HTTPException(status_code=404, detail=x)` -> `NotFound(x)`.

        The verdict comes from `http_exception_rule`, the same function the
        report uses, so a status wreath ships no class for is annotated on both
        sides rather than reported translated here and skipped there. When the
        rewrite does not happen the *name* survives, and `visit_Name` sees that
        and keeps an import for it — pointed at `wreath.exceptions`, whose
        `HTTPException` is the base class of every class in this table.
        """
        rule_id = http_exception_rule(self.imports, node)
        if rule_id != "exc.http_literal":
            self._annotate(node.lineno, rule_id)
            return
        status = status_int(self.imports, http_exception_status(node))
        # `http_exception_rule` returned `exc.http_literal`, so the status is an
        # int this table has a class for. Read rather than asserted: `-O` strips
        # an assert, and a wrong lookup here would emit a call to a name that
        # does not exist.
        cls = STATUS_EXCEPTION[status] if status is not None else None
        if cls is None:  # pragma: no cover - unreachable while the rule agrees
            self._annotate(node.lineno, "exc.http_unmapped")
            return
        detail = next((kw.value for kw in node.keywords if kw.arg == "detail"), None)
        if detail is None and len(node.args) > 1:
            detail = node.args[1]
        self.needs.add(cls)
        self._rewritten.add(id(node.func))
        detail_src = self._seg(detail) if detail is not None else ""
        self._replace_all_of(node, f"{cls}({detail_src})")

    def _rewrite_cache_call(self, node: ast.Call, func: ast.expr) -> None:
        """`TTLCache(maxsize=500, ttl=60)` -> `BoundedCache(max_entries=500, ttl=60)`.

        The same bounded LRU with the same eviction, under the framework's own
        memory budget. `LRUCache`/`FIFOCache`/`LFUCache` land on it too: wreath
        has one bounded cache, and the eviction order is the part that changes.
        """
        self.needs.add("BoundedCache")
        self._rewritten.add(id(func))
        self._replace_all_of(func, "BoundedCache")
        for keyword in node.keywords:
            if keyword.arg == "maxsize":
                start = self.buf.start_of(keyword)
                self.buf._edits.append((start, start + len("maxsize"), b"max_entries"))

    def _rewrite_response_call(self, node: ast.Call, wreath_name: str) -> None:
        """Bring a response constructor's arguments over with its name.

        The class is a rename; its arguments are not quite. Wreath calls the
        status `status`, and takes the body as the first argument rather than
        `content=`. Renaming the import without these would have produced a
        handler that raises `TypeError` the first time it answers — which is
        exactly the sort of "translated" that is worse than a note.
        """
        rename = {"status_code": "status", "content": _RESPONSE_BODY_ARG[wreath_name]}
        for keyword in node.keywords:
            if keyword.arg not in rename:
                continue
            # An in-place rename of the keyword only. Rebuilding the call would
            # have been simpler to write and was wrong twice over: it reordered
            # `JSONResponse(status_code=…, content=…)` into a positional
            # argument after a keyword one, and it re-copied the argument source
            # over the top of edits already made inside it.
            start = self.buf.start_of(keyword)
            self.buf._edits.append(
                (start, start + len(keyword.arg or ""), rename[keyword.arg].encode())
            )
        unmapped = sorted(
            keyword.arg
            for keyword in node.keywords
            if keyword.arg not in rename and keyword.arg not in ("background", "status")
        )
        if unmapped:
            self._note(
                node.lineno,
                "resp.class",
                f"{wreath_name} has no "
                + ", ".join(f"{name}=" for name in unmapped)
                + ". Headers go in as a list of lowercase byte pairs, "
                '`[(b"x-total", b"12")]`, and the content type comes from the '
                "response class itself -- so move these across or drop them",
            )

    def _rewrite_add_middleware(self, node: ast.Call) -> None:
        function = node.func
        if not isinstance(function, ast.Attribute):
            raise RuntimeError("add_middleware rewrite requires an attribute call")
        if not node.args:
            return
        first = node.args[0]
        tail = self.imports.origin(first).split(".")[-1]
        policy = {
            "CORSMiddleware": ("cors", "CorsPolicy"),
            "TrustedHostMiddleware": ("trusted_host", "TrustedHostPolicy"),
        }.get(tail)
        if tail == "RateLimitingMiddleware" and self._rewrite_rate_limit_middleware(node):
            return
        if policy is None:
            self._annotate(node.lineno, "mw.custom")
            return
        field, class_name = policy
        if class_name == "TrustedHostPolicy":
            allowed = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "allowed_hosts"),
                None,
            )
            if (
                isinstance(allowed, (ast.List, ast.Tuple))
                and len(allowed.elts) == 1
                and isinstance(allowed.elts[0], ast.Constant)
                and allowed.elts[0].value == "*"
            ):
                self._replace_all_of(node, "")
                return
        self.needs.update({"HttpPolicy", class_name})
        arguments = [self._seg(argument) for argument in node.args[1:]]
        arguments.extend(f"{kw.arg}={self._seg(kw.value)}" for kw in node.keywords)
        receiver = self._seg(function.value)
        configured = f"{class_name}({', '.join(arguments)})"
        self.buf.replace(
            node,
            f"{receiver}.configure_http_policy(HttpPolicy({field}={configured}))",
        )

    def _rewrite_rate_limit_middleware(self, node: ast.Call) -> bool:
        provider = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "provider"),
            None,
        )
        if not (
            isinstance(provider, ast.Call)
            and (
                provider.func.attr
                if isinstance(provider.func, ast.Attribute)
                else getattr(provider.func, "id", "")
            )
            == "InMemoryLimitProvider"
        ):
            return False
        options = {keyword.arg: keyword.value for keyword in provider.keywords}
        if "limit" not in options or "timespan" not in options:
            return False
        arguments = [
            f"limit={self._seg(options['limit'])}",
            f"window={self._seg(options['timespan'])}",
        ]
        included = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "included_routes"),
            None,
        )
        if included is not None:
            routes = self._seg(included)
            arguments.append(
                "exempt=lambda request: not any("
                f"request.path.startswith(prefix) for prefix in {routes})"
            )
        receiver = self._seg(node.func.value) if isinstance(node.func, ast.Attribute) else "app"
        self.needs.update({"HttpPolicy", "RateLimitPolicy"})
        self._removed_middleware_imports.update({"RateLimitingMiddleware", "InMemoryLimitProvider"})
        self._replace_all_of(
            node,
            f"{receiver}.configure_http_policy(HttpPolicy("
            f"rate_limit=RateLimitPolicy({', '.join(arguments)})))",
        )
        blocked = options.get("block_duration")
        if not (blocked is None or isinstance(blocked, ast.Constant) and blocked.value is None):
            self._note(
                node.lineno,
                "mw.custom",
                "the fixed-window limiter became Wreath's token bucket with the same "
                "limit and window. Its block_duration has no equivalent: refusals now "
                "carry Retry-After for the bucket's actual refill time",
            )
        else:
            self._resolve(node.lineno, "mw.custom")
        return True
