"""Everything a relay service does off the request path.

`kelpbed_relay` is the corpus authority for the four constructs that live
outside a handler: a Celery queue, an unsupervised drain loop, a per-call httpx
client, and a webhook signature checked by hand. Each has a shipped wreath
target, and none of the four is a rewrite the emitter performs on its own, so
all four are needs-review with a name attached.
"""

from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def relay_root() -> Path:
    return Path(__file__).parent / "corpus" / "kelpbed_relay"


def _findings(relay_root: Path, name: str) -> list:
    return [f for f in port.analyze(relay_root).findings if f.file == name]


def test_every_celery_spelling_in_the_module_is_billed(relay_root: Path) -> None:
    """`@shared_task`, `@relay.task` and `.delay()` are three sites, not two.

    The bare `@relay.task` is the one that used to vanish: the gate read the
    variable's *name* for the string "celery", so a runner called `relay`
    reported nothing while the emitter rewrote the decorator anyway.
    """
    celery = [
        f for f in _findings(relay_root, "tasks.py") if f.rule_id.startswith("bg.celery")
    ]
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
    """Nothing holds the handle, so nothing observes the exception that ends it."""
    (finding,) = [f for f in _findings(relay_root, "watch.py") if f.construct == "background"]
    assert finding.rule_id == "bg.asyncio_loop"
    assert finding.tag == port.NEEDS_REVIEW
    assert "bg.asyncio_joined" != finding.rule_id


def test_a_client_per_call_names_the_managed_client(relay_root: Path) -> None:
    """Two `AsyncClient(...)` sites, one finding: the library is the decision.

    Billing every construction would report one dependency as a list of call
    sites; the port is to hand connection lifetime to the app once.
    """
    httpx = [f for f in _findings(relay_root, "upstream.py") if f.rule_id == "ext.httpx"]
    assert len(httpx) == 1
    assert httpx[0].tag == port.NEEDS_REVIEW
    assert "app.http_client" in httpx[0].message


def test_a_correct_digest_check_is_still_a_replayable_webhook(relay_root: Path) -> None:
    """The comparison is right, which is exactly why the verdict is not "fine".

    `hmac.compare_digest` over the correct digest is the part porters get
    right; the timestamp window and the replay memory are the parts that are
    absent here and shipped in `wreath.webhooks`.
    """
    (finding,) = [f for f in _findings(relay_root, "inbound.py") if f.rule_id == "webhook.hmac"]
    assert finding.tag == port.NEEDS_REVIEW
    assert "replay" in finding.message
    assert "do not port the comparison" in finding.message
