"""Background jobs via Celery (a different background stack from tumbleweed_api).

Exercises the Celery-task translation rules: ``@task(bind=True, max_retries=,
default_retry_delay=)``, ``self.retry``, ``.delay(...)`` call sites.
"""
from celery import Celery

from .settings import settings

celery_app = Celery("roost", broker=settings.broker_url, backend=settings.result_backend)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=10)
def send_boarding_reminder(self, booking_id: str) -> None:
    try:
        _deliver(booking_id)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task
def nightly_fleece_report() -> dict:
    return {"generated": True}


def _deliver(booking_id: str) -> None:
    pass
