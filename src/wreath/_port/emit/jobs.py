"""Work that happens off the request: Celery tasks, `BackgroundTasks`, and the
bare `asyncio.create_task` that nothing joins."""

from __future__ import annotations

import ast

from ..analyzer import _is_true
from .buffer import _ends_argument_list
from .state import _EmitterState


class _BackgroundWork(_EmitterState):
    # -- Celery -> wreath jobs -------------------------------------------------

    def _rewrite_celery_app(self, stmt: ast.Assign, call: ast.Call) -> None:
        """`x = Celery(...)` -> `x = app.jobs("x", database="...")`.

        The broker and backend arguments go: wreath's queue is the database, so
        there is no broker URL to carry. The database name is the one thing that
        cannot be read off the Celery call, so it is emitted as a placeholder
        and noted -- the alternative is inventing a name that has to be right.
        """
        if not isinstance(stmt.targets[0], ast.Name):
            self._annotate(stmt.lineno, "bg.celery")
            return
        name = stmt.targets[0].id
        queue = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else name
        self._replace_all_of(call, f'app.jobs("{queue}", database="DATABASE")')
        self._note(
            stmt.lineno,
            "bg.celery",
            'name the database this queue runs on -- wreath\'s queue is a table on '
            "an app.postgres() database, so there is no broker URL and nothing in "
            "the Celery call says which database it should be",
        )

    def _rewrite_celery_task(self, node, dec: ast.expr, runner: str) -> None:
        """`@x.task(...)` -> `@x.task("name", retries=..., timeout=...)`.

        Celery passes the task instance as `self` under `bind=True`; wreath
        passes a context as the first parameter, so the rename is the same edit.
        """
        kwargs = []
        bind = False
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "bind":
                    bind = _is_true(kw.value)
                elif kw.arg == "max_retries":
                    # Both count *retries*, not attempts: wreath's
                    # `max_attempts = retries + 1` (`jobs.py:437`) is Celery's
                    # arithmetic, so the number carries across untouched.
                    kwargs.append(f"retries={self._seg(kw.value)}")
                elif kw.arg in ("time_limit", "soft_time_limit", "task_soft_time_limit"):
                    kwargs.append(f"timeout={self._seg(kw.value)}")
                elif kw.arg == "default_retry_delay":
                    # A fixed number of seconds between attempts. Wreath's
                    # default is exponential, so the base alone would change the
                    # schedule -- `backoff="fixed"` is what makes it the same
                    # delay every time, which is what Celery did.
                    kwargs.append('backoff="fixed"')
                    kwargs.append(f"backoff_base={self._seg(kw.value)}")
        args = "".join(f", {kw}" for kw in kwargs)
        self._replace_all_of(dec, f'{runner}.task("{node.name}"{args})')
        first = node.args.args[0] if node.args.args else None
        if bind and first is not None and first.arg == "self":
            self.buf.replace(first, "ctx")
        elif first is None or first.arg != "ctx":
            if node.args.args:
                begin = self.buf.start_of(node.args.args[0])
                self.buf._edits.append((begin, begin, b"ctx, "))
            else:
                self._note(
                    node.lineno, "bg.celery",
                    "add the `ctx` first parameter: a wreath handler is "
                    "`async def handler(ctx, *args)`",
                )
        if not isinstance(node, ast.AsyncFunctionDef):
            self._note(
                node.lineno, "bg.celery",
                "wreath job handlers are async; make this `async def` and await "
                "the database calls inside it",
            )
        # The finding sits on the decorator; resolving only the def line left
        # the generic "celery has a replacement" note above a call already
        # rewritten into that replacement.
        for rule_id in ("bg.celery", "bg.celery.task"):
            self._resolve(node.lineno, rule_id)
            self._resolve(getattr(dec, "lineno", node.lineno), rule_id)

    def _rewrite_celery_enqueue(self, node: ast.Call) -> None:
        """`task.delay(a)` / `task.apply_async(args=[a])` -> `await x.enqueue("task", a)`.

        The runner is the one the task's own decorator named. Where this module
        cannot see that decorator the call keeps its note: writing `enqueue` on a
        runner picked by guesswork would send the job to a queue nothing reads.
        """
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return
        task = func.value.id
        runner = self._celery_task_runners.get(task)
        if runner is None:
            return
        if func.attr == "delay":
            arguments = [self._seg(argument) for argument in node.args]
        else:
            listed = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "args"), None
            )
            if not isinstance(listed, (ast.List, ast.Tuple)):
                return
            arguments = [self._seg(element) for element in listed.elts]
        joined = "".join(f", {argument}" for argument in arguments)
        text = f'{runner}.enqueue("{task}"{joined})'
        awaited = self._parents.get(id(node))
        if not isinstance(awaited, ast.Await):
            text = f"await {text}"
        self._replace_all_of(node, text)
        self._resolve(node.lineno, "bg.celery.enqueue")

    def _rewrite_route_background_tasks(self, node) -> None:
        parameter = next(
            (
                argument
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                if argument.annotation is not None
                and self.imports.origin(argument.annotation) == "fastapi.BackgroundTasks"
            ),
            None,
        )
        if parameter is None:
            return
        returns: list[ast.Return] = []
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Return) or candidate.value is None:
                continue
            owner = self._parents.get(id(candidate))
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = self._parents.get(id(owner))
            if owner is node:
                returns.append(candidate)
        if not returns or any(
            not isinstance(statement.value, ast.Call)
            or not (
                (
                    statement.value.func.attr
                    if isinstance(statement.value.func, ast.Attribute)
                    else getattr(statement.value.func, "id", "")
                ).endswith("Response")
            )
            for statement in returns
        ):
            return
        self._remove_function_parameter(node, parameter)
        body_index = (
            1
            if node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
            and len(node.body) > 1
            else 0
        )
        first = node.body[body_index]
        indent = self.buf.line_indent(first.lineno)
        self.buf.insert_before_line(
            first.lineno, f"{indent}{parameter.arg} = BackgroundTasks()"
        )
        for statement in returns:
            value = statement.value
            if value is None:  # filtered when `returns` was built; re-narrowed here
                continue
            close = self.buf.end_of(value) - 1
            separator = "" if _ends_argument_list(self.buf.b, close) else ", "
            self.buf._edits.append(
                (close, close, f"{separator}background={parameter.arg}".encode())
            )
        self.needs.update({"BackgroundTasks"})

    def _rewrite_route_created_tasks(self, node) -> None:
        if any(
            argument.annotation is not None
            and self.imports.origin(argument.annotation) == "fastapi.BackgroundTasks"
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ):
            return
        calls: list[tuple[ast.Call, ast.Call]] = []
        for candidate in ast.walk(node):
            if not (
                isinstance(candidate, ast.Call)
                and self.imports.origin(candidate.func) == "asyncio.create_task"
                and candidate.args
                and isinstance(candidate.args[0], ast.Call)
                and isinstance(self._parents.get(id(candidate)), ast.Expr)
                and all(keyword.arg == "name" for keyword in candidate.keywords)
            ):
                continue
            owner = self._parents.get(id(candidate))
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = self._parents.get(id(owner))
            if owner is node:
                calls.append((candidate, candidate.args[0]))
        if not calls:
            return
        name = "_wreath_background"
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and len(node.body) > 1
        ):
            first = node.body[1]
        indent = self.buf.line_indent(first.lineno)
        self.buf.insert_before_line(first.lineno, f"{indent}{name} = BackgroundTasks()")
        for task, coroutine in calls:
            arguments = [self._seg(argument) for argument in coroutine.args]
            arguments.extend(
                f"{keyword.arg}={self._seg(keyword.value)}"
                for keyword in coroutine.keywords
            )
            suffix = "" if not arguments else ", " + ", ".join(arguments)
            self._replace_all_of(
                task,
                f"{name}.add_task({self._seg(coroutine.func)}{suffix})",
            )
            self._resolve(task.lineno, "bg.asyncio_loop")
        returns: list[ast.Return] = []
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Return):
                continue
            owner = self._parents.get(id(candidate))
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = self._parents.get(id(owner))
            if owner is node:
                returns.append(candidate)
        if not returns:
            return
        for statement in returns:
            value = statement.value
            if value is None:
                self._replace_all_of(
                    statement, f"return Response(status=204, background={name})"
                )
                self.needs.add("Response")
                continue
            response_name = (
                value.func.attr
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                else getattr(value.func, "id", "") if isinstance(value, ast.Call) else ""
            )
            if response_name.endswith("Response"):
                close = self.buf.end_of(value) - 1
                separator = "" if _ends_argument_list(self.buf.b, close) else ", "
                self.buf._edits.append(
                    (close, close, f"{separator}background={name}".encode())
                )
            else:
                self._replace_all_of(
                    value, f"JSONResponse({self._seg(value)}, background={name})"
                )
                self.needs.add("JSONResponse")
        self.needs.add("BackgroundTasks")
