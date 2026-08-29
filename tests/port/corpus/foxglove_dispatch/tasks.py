"""Foxglove dispatch — the Celery wiring, which is a rename plus one gap.

The target for `bg.celery` is exact, but the rewrite is not implemented. This
module carries each half separately.

The one place the rename stops is `countdown=`. `JobRunner.enqueue` takes
`run_at=`, an absolute instant — `jobs.py:686` — and `COALESCE($5, now())` is
the SQL behind it. There is no seconds-from-now parameter, so a countdown
becomes an arithmetic expression the porter would have to author.

`beat_schedule` with `crontab(...)` maps onto `runner.schedule(task, cron=...)`,
whose grammar `_jobcore.py:178` gives as "minute hour day-of-month month
day-of-week" with `*`, lists, ranges and steps — and which is **read in UTC**,
where Celery's crontab honours `CELERY_TIMEZONE`. Same expression, possibly a
different hour.
"""

from celery import Celery
from celery.schedules import crontab

dispatch = Celery("foxglove", broker="amqp://queue.foxglove.invalid//")


@dispatch.task
def rebuild_index(catchment_id: str) -> None:
    _rebuild(catchment_id)


@dispatch.task(bind=True, max_retries=3, default_retry_delay=60)
def push_manifest(self, manifest_id: str) -> None:
    try:
        _push(manifest_id)
    except TimeoutError as exc:
        raise self.retry(exc=exc)


@dispatch.task(queue="reports", time_limit=20)
def nightly_rollup() -> dict:
    return {"rolled_up": True}


def request_rebuild(catchment_id: str) -> None:
    rebuild_index.delay(catchment_id)


def request_push(manifest_id: str) -> None:
    push_manifest.apply_async(args=[manifest_id])


def request_delayed_push(manifest_id: str) -> None:
    push_manifest.apply_async(args=[manifest_id], countdown=30)


dispatch.conf.beat_schedule = {
    "nightly-rollup": {
        "task": "foxglove_dispatch.tasks.nightly_rollup",
        "schedule": crontab(hour=3, minute=0),
    },
    "five-minute-sweep": {
        "task": "foxglove_dispatch.tasks.rebuild_index",
        "schedule": crontab(minute="*/5"),
        "args": ("all",),
    },
}


def _rebuild(catchment_id: str) -> None:
    raise NotImplementedError("the index builder lives in the deployment image")


def _push(manifest_id: str) -> None:
    raise NotImplementedError("the downstream client lives in the deployment image")
