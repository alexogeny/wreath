"""The module pre-scan, and the statement dispatchers.

`visit_Module` runs first and collects what a node cannot see from itself:
which `with httpx.AsyncClient()` blocks are rewritable, which names hold a
test client, what each settings class binds."""

from __future__ import annotations

import ast

from ..analyzer import celery_runner_names, lifespan_names, parent_map
from .http_client import _HttpxRewrite
from .jobs import _BackgroundWork
from .targets import _TEST_REQUEST_METHODS
from .testing import _TestClient


class _ModuleWalk(_TestClient, _HttpxRewrite, _BackgroundWork):
    def visit_Module(self, node: ast.Module) -> None:
        self._parents = parent_map(node)
        self._used_names = {
            candidate.id for candidate in ast.walk(node) if isinstance(candidate, ast.Name)
        }
        class_bases = {
            candidate.name: {
                self._seg(base.func.value)
                if (
                    isinstance(base, ast.Call)
                    and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "model_as_partial"
                )
                else self._seg(base).split("[", 1)[0]
                for base in candidate.bases
            }
            for candidate in node.body
            if isinstance(candidate, ast.ClassDef)
        }
        partial: set[str] = set()
        for candidate in node.body:
            if not isinstance(candidate, ast.ClassDef):
                continue
            for base in candidate.bases:
                if (
                    isinstance(base, ast.Call)
                    and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "model_as_partial"
                ):
                    partial.add(candidate.name)
                    partial.add(self._seg(base.func.value))
        children: dict[str, set[str]] = {}
        for name, bases in class_bases.items():
            for base in bases:
                children.setdefault(base, set()).add(name)
        pending = list(partial)
        while pending:
            name = pending.pop()
            related = children.get(name, set()) | {
                base for base in class_bases.get(name, ()) if base in class_bases
            }
            for relative in related - partial:
                partial.add(relative)
                pending.append(relative)
        self._pydantic_partial_family = frozenset(partial)

        def owner_id(candidate: ast.AST) -> int | None:
            owner = self._parents.get(id(candidate))
            while owner is not None:
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return id(owner)
                owner = self._parents.get(id(owner))
            return None

        def owner_chain(candidate: ast.AST) -> tuple[int, ...]:
            owners: list[int] = []
            owner = self._parents.get(id(candidate))
            while owner is not None:
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owners.append(id(owner))
                owner = self._parents.get(id(owner))
            return tuple(owners)

        transport_definitions: dict[str, list[tuple[ast.Assign, ast.Call, int | None]]] = {}
        for candidate in ast.walk(node):
            if (
                isinstance(candidate, ast.Assign)
                and len(candidate.targets) == 1
                and isinstance(candidate.targets[0], ast.Name)
                and isinstance(candidate.value, ast.Call)
                and self.imports.origin(candidate.value.func) == "httpx.AsyncHTTPTransport"
            ):
                transport_definitions.setdefault(candidate.targets[0].id, []).append(
                    (candidate, candidate.value, owner_id(candidate))
                )
            if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                continue
            target = (
                candidate.target
                if isinstance(candidate, ast.AnnAssign)
                else candidate.targets[0]
                if len(candidate.targets) == 1
                else None
            )
            value = candidate.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Call)
                and self._http_timeout_total(value) is not None
            ):
                self._http_timeout_constants.add(target.id)
        self._lifespan_names = lifespan_names(node)
        # `rebuild_index.delay(x)` names the *task*, and the runner it belongs to
        # is on the decorator above the task's own `def`. Collected here, not at
        # the call, because the enqueue is routinely written above the task.
        runners = celery_runner_names(node, self.imports)
        self._celery_task_runners = {
            candidate.name: target.value.id
            for candidate in ast.walk(node)
            if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in candidate.decorator_list
            for target in (decorator.func if isinstance(decorator, ast.Call) else decorator,)
            if isinstance(target, ast.Attribute)
            and target.attr == "task"
            and isinstance(target.value, ast.Name)
            and target.value.id in runners
        }
        self._settings_custom_init = frozenset(
            cls.name
            for cls in node.body
            if isinstance(cls, ast.ClassDef)
            and cls.name in self.settings_models
            and any(
                isinstance(statement, ast.FunctionDef)
                and statement.name == "__init__"
                and bool(
                    statement.args.posonlyargs
                    or statement.args.kwonlyargs
                    or len(statement.args.args) > 1
                    or statement.args.vararg
                    or statement.args.kwarg
                )
                for statement in cls.body
            )
        )
        for cls in node.body:
            if not isinstance(cls, ast.ClassDef) or cls.name not in self.settings_models:
                continue
            env_file = prefix = None
            for statement in cls.body:
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                if not any(
                    isinstance(target, ast.Name) and target.id == "model_config"
                    for target in targets
                ) or not isinstance(statement.value, ast.Call):
                    continue
                for keyword in statement.value.keywords:
                    if keyword.arg == "env_file":
                        env_file = self._seg(keyword.value)
                    elif keyword.arg == "env_prefix":
                        prefix = self._seg(keyword.value)
            self._settings_bindings[cls.name] = (env_file, prefix)
        for candidate in ast.walk(node):
            if not isinstance(candidate, (ast.With, ast.AsyncWith)):
                continue
            for item in candidate.items:
                call = item.context_expr
                if not (
                    isinstance(call, ast.Call)
                    and self.imports.origin(call.func) == "httpx.AsyncClient"
                    and isinstance(item.optional_vars, ast.Name)
                    and not call.args
                ):
                    continue
                key = id(item)
                options = {keyword.arg for keyword in call.keywords}
                if None in options or not options <= {
                    "base_url",
                    "headers",
                    "timeout",
                    "transport",
                }:
                    continue
                transport = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "transport"),
                    None,
                )
                if transport is not None:
                    transport_assignment = None
                    if isinstance(transport, ast.Name):
                        scopes = owner_chain(call)
                        candidates = [
                            (assignment, definition)
                            for assignment, definition, scope in transport_definitions.get(
                                transport.id, ()
                            )
                            if assignment.lineno < call.lineno
                            and (scope in scopes or (scope is None and not scopes))
                        ]
                        if candidates:
                            transport_assignment, transport = max(
                                candidates, key=lambda item: item[0].lineno
                            )
                    if not (
                        isinstance(transport, ast.Call)
                        and self.imports.origin(transport.func) == "httpx.AsyncHTTPTransport"
                        and not transport.args
                        and all(keyword.arg == "retries" for keyword in transport.keywords)
                    ):
                        continue
                    retries = next(
                        (
                            keyword.value
                            for keyword in transport.keywords
                            if keyword.arg == "retries"
                        ),
                        None,
                    )
                    if retries is not None:
                        self._http_retries[key] = retries
                        if transport_assignment is not None:
                            self._http_transport_assignments.add(id(transport_assignment))
                requests = [
                    request
                    for request in ast.walk(candidate)
                    if isinstance(request, ast.Call)
                    and isinstance(request.func, ast.Attribute)
                    and isinstance(request.func.value, ast.Name)
                    and request.func.value.id == item.optional_vars.id
                    and request.func.attr in _TEST_REQUEST_METHODS
                    and all(
                        keyword.arg in {"headers", "json", "content", "data", "params", "timeout"}
                        for keyword in request.keywords
                    )
                ]
                if len(requests) == 1:
                    request_timeout = next(
                        (
                            keyword.value
                            for keyword in requests[0].keywords
                            if keyword.arg == "timeout"
                        ),
                        None,
                    )
                    if request_timeout is not None:
                        self._http_request_timeouts[key] = request_timeout
                if "base_url" not in options:
                    if len(requests) != 1:
                        continue
                    request = requests[0]
                    # Re-narrowed: the comprehension above only keeps calls whose
                    # `func` is an Attribute, but `requests` is a list of `ast.Call`
                    # and that guarantee does not survive the binding.
                    method = request.func.attr if isinstance(request.func, ast.Attribute) else ""
                    target_index = 1 if method == "request" else 0
                    if len(request.args) <= target_index:
                        continue
                    self._http_dynamic_clients[key] = request.args[target_index]
                headers = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "headers"),
                    None,
                )
                self._http_clients[key] = (item.optional_vars.id, headers)
                self._http_client_calls[id(call)] = key
                self._http_requests.update((id(request), key) for request in requests)
        for candidate in ast.walk(node):
            if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                continue
            value = candidate.value
            if not (
                isinstance(value, ast.Await)
                and isinstance(value.value, ast.Call)
                and id(value.value) in self._http_requests
            ):
                continue
            targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
            self._http_responses.update(
                (owner_id(candidate), target.id)
                for target in targets
                if isinstance(target, ast.Name)
            )
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and self._is_test_client_call(statement.value)
            ):
                self._global_test_clients[statement.targets[0].id] = statement.value
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fixture = any(
                self.imports.origin(
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                ).endswith("pytest.fixture")
                or (isinstance(decorator, ast.Attribute) and decorator.attr == "fixture")
                for decorator in statement.decorator_list
            )
            if fixture and any(
                isinstance(inner, (ast.Return, ast.Yield))
                and isinstance(inner.value, ast.Call)
                and self._is_test_client_call(inner.value)
                for inner in ast.walk(statement)
            ):
                self._fixture_test_clients.add(statement.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            isinstance(node.value, ast.Call)
            and self.imports.origin(node.value.func).split(".")[-1] == "Celery"
            and node.targets
            and isinstance(node.targets[0], ast.Name)
        ):
            self._celery_runners.add(node.targets[0].id)
            self._rewrite_celery_app(node, node.value)
        if id(node) in self._http_transport_assignments:
            self._replace_all_of(node, "")
            return
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in self._global_test_clients
            and node.value is self._global_test_clients[node.targets[0].id]
        ):
            name = node.targets[0].id
            call = self._test_client_text(node.value)
            self.needs_fixture = True
            self._replace_all_of(
                node,
                f"@fixture\nasync def {name}():\n"
                f"    async with {call} as {name}:\n"
                f"        yield {name}",
            )
            self._resolve(node.lineno, "test.client")
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        value = node.value
        if (
            self.opinionated
            and isinstance(node.target, ast.Name)
            and isinstance(value, ast.Call)
            and (total := self._http_timeout_total(value)) is not None
        ):
            timeout_annotations = [
                candidate
                for candidate in ast.walk(node.annotation)
                if isinstance(candidate, (ast.Name, ast.Attribute))
                and self.imports.origin(candidate) == "httpx.Timeout"
            ]
            if timeout_annotations:
                self.needs.add("ClientTimeout")
                for annotation in timeout_annotations:
                    self._rewritten.update(id(item) for item in ast.walk(annotation))
                    self.buf.replace(annotation, "ClientTimeout")
                self._replace_all_of(value, f"ClientTimeout(total={total})")
                self._resolve(node.lineno, "ext.httpx")
                return
        self.generic_visit(node)
