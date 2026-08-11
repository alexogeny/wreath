"""Frond re-sampling runs off the request path.

Celery the way a service of this size spells it: a ``shared_task`` with a retry
policy for the per-transect work, a bare ``@relay.task`` for the nightly
roll-up, and ``.delay()`` at the call site.
"""
from celery import Celery, shared_task

relay = Celery("kelpbed", broker="amqp://queue.kelpbed.invalid//")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def resample_transect(self, transect_id: str) -> None:
    try:
        _measure(transect_id)
    except TimeoutError as exc:
        raise self.retry(exc=exc)


@relay.task
def nightly_biomass_rollup() -> dict:
    return {"rolled_up": True}


def request_resample(transect_id: str) -> None:
    resample_transect.delay(transect_id)


def _measure(transect_id: str) -> None:
    raise NotImplementedError("the sonde driver lives in the deployment image")
