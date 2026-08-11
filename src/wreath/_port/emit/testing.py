"""The test suite's client: `TestClient(app)`, the httpx/ASGI spelling of the
same thing, and the fixtures that hand one out."""

from __future__ import annotations

import ast

from .state import _EmitterState
from .targets import _TESTCLIENT_MODULES


class _TestClient(_EmitterState):
    def _asgi_test_client_app(self, call: ast.Call) -> ast.expr | None:
        if self.imports.origin(call.func) != "httpx.AsyncClient":
            return None
        options = {keyword.arg for keyword in call.keywords}
        if None in options or not options <= {"transport", "base_url", "headers"}:
            return None
        transport = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "transport"),
            None,
        )
        if not (
            isinstance(transport, ast.Call)
            and self.imports.origin(transport.func) == "httpx.ASGITransport"
        ):
            return None
        app = next(
            (keyword.value for keyword in transport.keywords if keyword.arg == "app"),
            transport.args[0] if transport.args else None,
        )
        return app if isinstance(app, ast.expr) else None

    def _is_test_client_call(self, call: ast.Call) -> bool:
        return (
            self.imports.origin(call.func)
            in {f"{module}.TestClient" for module in _TESTCLIENT_MODULES}
            or self._asgi_test_client_app(call) is not None
        )

    def _test_client_text(self, call: ast.Call) -> str:
        app = self._asgi_test_client_app(call)
        if app is None:
            return self._seg(call)
        self.needs.add("TestClient")
        headers = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "headers"),
            None,
        )
        suffix = "" if headers is None else f", headers={self._seg(headers)}"
        self._resolve(call.lineno, "ext.httpx")
        self._resolve(call.lineno, "test.client")
        self._rewritten.update(id(item) for item in ast.walk(call))
        return f"TestClient({self._seg(app)}{suffix})"

    def _add_test_fixture_param(self, node, name: str) -> None:
        existing = {
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        }
        if name in existing:
            return
        body_start = self.buf.start_of(node.body[0])
        close = self.buf.b.rfind(b")", self.buf.start_of(node), body_start)
        if close < 0:
            return
        has_args = bool(node.args.posonlyargs or node.args.args or node.args.kwonlyargs)
        if not has_args and node.args.vararg is None and node.args.kwarg is None:
            text = name
        elif node.args.vararg is not None or node.args.kwonlyargs:
            text = f", {name}"
        elif node.args.kwarg is not None:
            text = f"*, {name}, "
            close = self.buf.start_of(node.args.kwarg) - 2
        else:
            text = f", *, {name}"
        self.buf._edits.append((close, close, text.encode()))

    def _rewrite_test_client_fixture(self, node) -> None:
        if node.name not in self._fixture_test_clients:
            return
        changed = False
        for inner in ast.walk(node):
            if not (
                isinstance(inner, (ast.Return, ast.Yield))
                and isinstance(inner.value, ast.Call)
                and self._is_test_client_call(inner.value)
            ):
                continue
            statement = self._parents.get(id(inner)) if isinstance(inner, ast.Yield) else inner
            if not isinstance(statement, (ast.Expr, ast.Return)):
                continue
            indent = self.buf.line_indent(statement.lineno)
            call = self._test_client_text(inner.value)
            self._replace_all_of(
                statement,
                f"async with {call} as {node.name}:\n{indent}    yield {node.name}",
            )
            self._resolve(inner.value.lineno, "test.client")
            changed = True
        if changed and isinstance(node, ast.FunctionDef):
            self.buf._edits.append((self.buf.start_of(node), self.buf.start_of(node), b"async "))

    def _rewrite_test_client_function(self, node) -> frozenset[str]:
        """Make one local FastAPI TestClient an async lifespan context.

        The exact shape is deliberately narrow: one direct assignment in a
        function. Module globals, yielded fixtures and factory-returned clients
        have ownership outside this body, so the porter leaves those for a
        person instead of guessing where their lifespan ends.
        """
        if not self.opinionated:
            return frozenset()
        assignments: list[tuple[ast.Assign, ast.Name, ast.Call]] = []
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and self._is_test_client_call(statement.value)
            ):
                assignments.append((statement, statement.targets[0], statement.value))
        if len(assignments) != 1:
            return frozenset()
        statement, target, call = assignments[0]
        if isinstance(node, ast.FunctionDef):
            self.buf._edits.append((self.buf.start_of(node), self.buf.start_of(node), b"async "))
        self._replace_all_of(
            statement,
            f"async with {self._test_client_text(call)} as {target.id}:",
        )
        if node.end_lineno is None or statement.end_lineno is None:
            return frozenset()
        for line in range(statement.end_lineno + 1, node.end_lineno + 1):
            offset = self.buf.start_of_line(line)
            self.buf._edits.append((offset, offset, b"    "))
        self._resolve(call.lineno, "test.client_local")
        return frozenset((target.id,))
