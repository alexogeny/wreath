"""Which names in a module are a Celery application, and what that makes `@x.task`.

`origin()` answers "where did this name come from" by reading imports, and a
Celery application is not imported -- it is a local bound to a call. So the
resolved origin of `@relay.task` is the literal text `relay.task`, and asking
whether that contains "celery" tests the *variable's name*: `@celery_app.task`
was billed and `@relay.task`, `@app.task` and `@worker.task` were not, in a file
the emitter was rewriting all along. The binding is the fact; the name is a
coincidence.
"""

from __future__ import annotations

import ast

from .imports import _Imports

#: Celery `@task(...)` keyword -> how `JobRunner.task` spells the same thing
#: (`jobs.py:377`). `None` means wreath consumes it without a keyword of its own:
#: `bind=True` asks Celery to pass the task instance and wreath passes a `ctx`
#: to every handler regardless, so there is nothing left to carry.
#:
#: A keyword absent from this table is what keeps the verdict at needs-review.
#: `queue=` is the sharpest: wreath's queue *is* the runner, so a second queue is
#: a second `app.jobs(...)` and a decorator cannot express it. Guessing the two
#: apart is how a task quietly moves onto the wrong worker pool.
CELERY_TASK_KWARGS: dict[str, str | None] = {
    "bind": None,
    "max_retries": "retries",
    "time_limit": "timeout",
    "soft_time_limit": "timeout",
    "task_soft_time_limit": "timeout",
    "default_retry_delay": "backoff_base",
}

#: Celery `apply_async(...)` keyword -> its `JobRunner.enqueue` spelling
#: (`jobs.py:681`). `countdown=` is deliberately absent: `enqueue` takes
#: `run_at=`, an absolute instant, so a countdown is arithmetic the porter has to
#: author rather than a keyword that moves.
CELERY_ENQUEUE_KWARGS: dict[str, str] = {
    "queue": "",  # named here only to be recognized, never carried
    "priority": "priority",
    "task_id": "key",
}


def celery_task_rule(decorator: ast.expr, node) -> str:
    """The verdict one `@x.task` decorator earns.

    Shared with the emitter, so a decorator the report calls translated is one
    the emitter writes out. Two things hold it back, and both are in the source:
    a keyword with no `JobRunner.task` spelling, and a `self.retry()` in the
    body -- wreath retries by letting the handler raise, so there is no call to
    rename and deleting one is a change to what happens on failure.
    """
    if isinstance(decorator, ast.Call):
        if decorator.args:
            return "bg.celery"
        if any(
            keyword.arg is None or keyword.arg not in CELERY_TASK_KWARGS
            for keyword in decorator.keywords
        ):
            return "bg.celery"
    return "bg.celery" if _calls_retry(node) else "bg.celery.task"


def _calls_retry(node) -> bool:
    """Whether the handler body calls `self.retry(...)`, which has no wreath form."""
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "retry"
        and isinstance(inner.func.value, ast.Name)
        and inner.func.value.id == "self"
        for inner in ast.walk(node)
    )


def celery_enqueue_rule(call: ast.Call, *, inside_async: bool) -> str:
    """The verdict one `.delay(...)`/`.apply_async(...)` earns.

    `enqueue` is a coroutine, so the rewrite only lands where there is already
    an `await` to put it in. Inside a plain `def` the target is still exact and
    still written into the note -- what is missing is the caller's own signature,
    which is a change that leaves this file.
    """
    if not inside_async:
        return "bg.celery"
    if call.func.attr == "delay" if isinstance(call.func, ast.Attribute) else False:
        return "bg.celery.enqueue" if not call.keywords else "bg.celery"
    positional = list(call.args)
    if positional:
        return "bg.celery"
    for keyword in call.keywords:
        if keyword.arg == "args":
            if not isinstance(keyword.value, (ast.List, ast.Tuple)):
                return "bg.celery"
        elif keyword.arg not in CELERY_ENQUEUE_KWARGS or keyword.arg == "queue":
            return "bg.celery"
    return "bg.celery.enqueue"


def celery_runner_names(module: ast.Module, imports: _Imports) -> frozenset[str]:
    """Every name in this module bound to a `Celery(...)` call.

    The same question `emit/walk.py` asks before it rewrites `@x.task`, so the
    report and the ported file agree about which decorators are tasks. Walked
    rather than read off `module.body`, because a runner built inside an
    `if`/`try` (the settings-dependent broker) is still the module's runner.
    """
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not (
            isinstance(node.value, ast.Call)
            and imports.origin(node.value.func).split(".")[-1] == "Celery"
        ):
            continue
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names)


def is_celery_task(
    decorator: ast.expr, origin: str, runners: frozenset[str], imports: _Imports
) -> bool:
    """Whether `@<something>.task` decorates a Celery task.

    Three ways to know, in falling order of how much they prove:

    * the attribute's base is a name this module bound to `Celery(...)`;
    * the resolved origin names celery, which is what an imported runner
      (`from .celery_app import celery_app`) leaves behind;
    * the module imports celery at all. That is the catch-all, and it is scoped
      deliberately: `worker = make_celery(app)` binds nothing this file can read,
      and without the scope the rule stops being a catch-all and becomes a match
      on the word "task". It is the same loose module-level question
      `serves_asgi` asks of a route decorator, for the same reason.
    """
    if origin.split(".")[-1] != "task":
        return False
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in runners
    ):
        return True
    return "celery" in origin.lower() or "celery" in imports.roots
