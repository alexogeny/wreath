"""Background work: Celery tasks, bare `asyncio` tasks and process pools."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

BACKGROUND: dict[str, tuple[str, str, str, str]] = {
    # Now portable to a SHIPPED wreath subsystem (was unsupported): reviewable, not
    # auto-translatable (the task/loop body is bespoke) — needs-review with a real target.
    "bg.celery": (
        "background",
        "other",
        NEEDS_REVIEW,
        "Celery has a replacement built in: app.jobs() with @jobs.task, and jobs.schedule(cron=...) for anything periodic. The wiring is a rename; the body of the task moves across as it is.",
    ),
    # The two halves of the Celery rename that are fully determined, split out so
    # a decorator the emitter has been rewriting all along stops being reported
    # as work still to do. What is left under `bg.celery` is what genuinely needs
    # a person: a `queue=` (a second runner), a `self.retry()` (no wreath form),
    # a `countdown=` (arithmetic on an instant), and the database name.
    "bg.celery.task": (
        "background",
        "other",
        TRANSLATED,
        '@x.task(...) becomes @x.task("<name>", ...) on a JobRunner: max_retries is retries, time_limit is timeout, default_retry_delay is backoff_base with backoff="fixed", and the handler gains the ctx first parameter wreath passes every job. The body moves across as it is.',
    ),
    "bg.celery.enqueue": (
        "background",
        "other",
        TRANSLATED,
        'delay(...) and apply_async(args=[...]) both become await runner.enqueue("<task>", *args). The arguments carry across positionally and nothing else about the call changes.',
    ),
    "bg.asyncio_loop": (
        "background",
        "other",
        NEEDS_REVIEW,
        "A loop started with asyncio.create_task has nothing supervising it -- if it raises, it stops and nothing says so. Move the work into app.jobs() or a supervised wreath service, which restarts it and reports failures.",
    ),
    "bg.asyncio_joined": (
        "background",
        "other",
        TRANSLATED,
        "This task is joined in the same async function that creates it, so it is structured request/test concurrency rather than a background service. Keep asyncio.create_task: its lifetime is already bounded and its exception is observed.",
    ),
    "bg.multiprocessing": (
        "background",
        "other",
        NEEDS_REVIEW,
        "Replace the worker process with jobs.launch(), and the shared file or table the client polls with progress reports. jobs.launch() hands back a task whose id is the job id, so the status endpoint and the progress stream need no second identifier. The body of the worker moves across as it is.",
    ),
}
