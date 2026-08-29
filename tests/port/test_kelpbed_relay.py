from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def relay_root() -> Path:
    return Path(__file__).parent / "corpus" / "kelpbed_relay"


def _findings(relay_root: Path, name: str) -> list:
    return [f for f in port.analyze(relay_root).findings if f.file == name]


def test_every_celery_spelling_in_the_module_is_billed(relay_root: Path) -> None:
    celery = [f for f in _findings(relay_root, "tasks.py") if f.rule_id.startswith("bg.celery")]
    assert [f.line for f in celery] == [12, 20, 26]
    # The three sites split by what is left to decide, not by spelling. The bare
    # `@relay.task` carries no keyword and its body calls nothing wreath has no
    # form for, so it is written out; the other two are held back by a
    # `self.retry()` (wreath retries by letting the handler raise) and by a
    # caller that would have to become `async def` to hold the `await`.
    assert [(f.line, f.rule_id) for f in celery] == [
        (12, "bg.celery"),
        (20, "bg.celery.task"),
        (26, "bg.celery"),
    ]
    assert all("app.jobs()" in f.message for f in celery if f.rule_id == "bg.celery")


def test_an_unjoined_drain_loop_is_not_structured_concurrency(relay_root: Path) -> None:
    (finding,) = [f for f in _findings(relay_root, "watch.py") if f.construct == "background"]
    assert finding.rule_id == "bg.asyncio_loop"
    assert finding.tag == port.NEEDS_REVIEW
    assert "bg.asyncio_joined" != finding.rule_id


def test_a_client_per_call_names_the_managed_client(relay_root: Path) -> None:
    httpx = [f for f in _findings(relay_root, "upstream.py") if f.rule_id == "ext.httpx"]
    assert len(httpx) == 1
    assert httpx[0].tag == port.NEEDS_REVIEW
    assert "app.http_client" in httpx[0].message


def test_a_correct_digest_check_is_still_a_replayable_webhook(relay_root: Path) -> None:
    (finding,) = [f for f in _findings(relay_root, "inbound.py") if f.rule_id == "webhook.hmac"]
    assert finding.tag == port.NEEDS_REVIEW
    assert "replay" in finding.message
    assert "do not port the comparison" in finding.message
