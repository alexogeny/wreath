"""The needs-review cluster, and the near-miss twin of each promotion.

`corpus/foxglove_dispatch/` is written as pairs: one construct in the form that
carries across on its own, and the same construct in the form that does not.
That is the specification, because the only cheap way to move a needs-review
number is to loosen the word -- and a rule that fires on both halves of a pair
has done exactly that.

Two of the pairs are settled here. `response_class=` is deletable when it names
the response wreath would have built anyway and not otherwise; `@x.task` is
written out when every keyword has a `JobRunner.task` spelling and the body has
no `self.retry()` to lose. The rest of the file stays needs-review on purpose,
and the last test says which and why.
"""
from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def foxglove(corpus_root):
    return corpus_root / "foxglove_dispatch"


def _rules(findings, name):
    return {(f.line, f.rule_id) for f in findings if f.file == name}


# -- response_class= ----------------------------------------------------------

def test_a_json_response_class_over_a_dict_return_is_the_default(foxglove) -> None:
    """`read_catchment` returns a dict, which wreath already sends as JSON.

    So `response_class=JSONResponse` is describing what happens anyway and the
    keyword is the whole edit.
    """
    assert (33, "route.response_class_default") in _rules(
        port.analyze(foxglove).findings, "api.py"
    )


def test_the_keyword_is_actually_deleted(foxglove, tmp_path) -> None:
    """`translated` has to mean the emitter wrote something.

    This rule sat in `docs/reference/port-gaps.md` under "not a gap, just
    unwritten" -- the target was exact and nothing performed it.
    """
    port.port_tree(foxglove, tmp_path)
    api = (tmp_path / "api.py").read_text(encoding="utf-8")

    decorators = [line for line in api.splitlines() if line.startswith("@router.")]

    assert '@router.get("/{catchment_id}")' in decorators
    assert not [line for line in decorators if "response_class" in line]


@pytest.mark.parametrize(
    ("decorator", "returned", "why"),
    [
        (
            "response_class=HTMLResponse",
            '    return "<p>ok</p>"\n',
            "wreath would send text/plain, so deleting the keyword changes the "
            "content type of every response",
        ),
        (
            "response_class=JSONResponse",
            '    return "ok"\n',
            'fastapi sends `"ok"` with the quotes as JSON and wreath sends `ok` '
            "as text/plain -- the body and the content type both change",
        ),
        (
            "response_class=PlainTextResponse",
            "    return {}\n",
            "wreath would send JSON, which is not what this route sends today",
        ),
    ],
)
def test_a_response_class_wreath_would_not_have_picked_stays_a_decision(
    tmp_path, decorator: str, returned: str, why: str
) -> None:
    (tmp_path / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        f'@router.get("/x", {decorator})\n'
        "async def read():\n"
        f"{returned}"
    )
    findings = port.analyze(tmp_path).findings
    (found,) = [f for f in findings if f.construct == "route_option"]

    assert found.rule_id == "route.response_class", why
    assert found.tag == port.NEEDS_REVIEW, why


def test_the_keyword_survives_when_it_was_not_a_no_op(tmp_path) -> None:
    """The other half of the promotion: nothing is deleted where it mattered."""
    (tmp_path / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from fastapi.responses import HTMLResponse\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        '@router.get("/x", response_class=HTMLResponse)\n'
        "async def read():\n"
        '    return "<p>ok</p>"\n'
    )
    out = tmp_path / "out"
    port.port_tree(tmp_path, out)

    assert "response_class=HTMLResponse" in (out / "api.py").read_text(encoding="utf-8")


# -- @x.task ------------------------------------------------------------------

def test_a_plain_task_decorator_is_written_out(foxglove) -> None:
    """`@dispatch.task` carries nothing that needs deciding."""
    assert (26, "bg.celery.task") in _rules(port.analyze(foxglove).findings, "tasks.py")


@pytest.mark.parametrize(
    ("line", "why"),
    [
        (31, "the body calls self.retry(), which wreath has no form for -- it "
             "retries by letting the handler raise, so there is no call to rename"),
        (39, "queue= is a second runner, not a keyword: wreath's queue *is* the "
             "JobRunner, so this needs an app.jobs() of its own"),
        (45, "the caller is a plain def and enqueue is a coroutine"),
        (49, "the caller is a plain def and enqueue is a coroutine"),
        (53, "countdown= is seconds from now and enqueue takes run_at=, an "
             "absolute instant -- the rewrite is arithmetic, not a rename"),
    ],
)
def test_each_held_back_celery_site_is_held_back_for_its_own_reason(
    foxglove, line: int, why: str
) -> None:
    assert (line, "bg.celery") in _rules(port.analyze(foxglove).findings, "tasks.py"), why


def test_a_task_with_every_keyword_mapped_carries_its_retry_policy(tmp_path) -> None:
    """The keywords are not dropped on the way across, and one of them is a pair.

    `default_retry_delay=` is a *fixed* number of seconds between attempts.
    Wreath's backoff is exponential by default, so carrying the base alone would
    change the schedule -- `backoff="fixed"` is what makes it the same wait.
    """
    (tmp_path / "tasks.py").write_text(
        "from celery import Celery\n"
        "\n"
        'dispatch = Celery("x")\n'
        "\n"
        "\n"
        "@dispatch.task(bind=True, max_retries=3, default_retry_delay=60, time_limit=20)\n"
        "async def push(self, manifest_id: str) -> None:\n"
        "    pass\n"
    )
    out = tmp_path / "out"
    port.port_tree(tmp_path, out)
    written = (out / "tasks.py").read_text(encoding="utf-8")

    assert (
        '@dispatch.task("push", retries=3, backoff="fixed", backoff_base=60, timeout=20)'
        in written
    )
    assert "async def push(ctx, manifest_id: str)" in written


def test_an_enqueue_in_an_async_caller_is_written_out(tmp_path) -> None:
    """`delay` and `apply_async(args=[...])` are the same call, spelled twice."""
    (tmp_path / "tasks.py").write_text(
        "from celery import Celery\n"
        "\n"
        'dispatch = Celery("x")\n'
        "\n"
        "\n"
        "@dispatch.task\n"
        "async def push(manifest_id: str) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "async def request(manifest_id: str) -> None:\n"
        "    push.delay(manifest_id)\n"
        "    push.apply_async(args=[manifest_id])\n"
    )
    out = tmp_path / "out"
    port.port_tree(tmp_path, out)
    written = (out / "tasks.py").read_text(encoding="utf-8")

    assert written.count('await dispatch.enqueue("push", manifest_id)') == 2
    assert ".delay(" not in written
    assert ".apply_async(" not in written


def test_an_enqueue_in_a_sync_caller_is_not_rewritten(tmp_path) -> None:
    """`enqueue` is a coroutine, so there has to be an `await` to put it in.

    Making the caller `async def` changes every one of *its* callers, which is
    the decision this hands back rather than makes.
    """
    (tmp_path / "tasks.py").write_text(
        "from celery import Celery\n"
        "\n"
        'dispatch = Celery("x")\n'
        "\n"
        "\n"
        "@dispatch.task\n"
        "async def push(manifest_id: str) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def request(manifest_id: str) -> None:\n"
        "    push.delay(manifest_id)\n"
    )
    out = tmp_path / "out"
    port.port_tree(tmp_path, out)
    written = (out / "tasks.py").read_text(encoding="utf-8")

    assert "push.delay(manifest_id)" in written
    assert "enqueue" not in written


# -- the pairs that stay -------------------------------------------------------

@pytest.mark.parametrize(
    ("line", "rule_id", "why"),
    [
        (60, "orm.query.filter", "first() with no order_by: 'the first row' is "
                                 "whatever postgres returned that day"),
        (70, "orm.query.get_or_create", "the default is computed at the call site"),
        (77, "exc.http_variable", "the status is computed, so no wreath class is named"),
    ],
)
def test_the_near_miss_half_of_each_pair_still_needs_a_person(
    foxglove, line: int, rule_id: str, why: str
) -> None:
    assert (line, rule_id) in _rules(port.analyze(foxglove).findings, "api.py"), why
