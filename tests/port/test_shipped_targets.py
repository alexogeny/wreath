"""Verdicts that changed because wreath shipped the thing they pointed away from.

A porting tool goes stale in one specific way: a rule says "no equivalent, keep
the library" long after the equivalent landed, and a porter keeps a dependency
they could have deleted. These pin the three that had gone stale, and the two
constructs that were invisible entirely.
"""
import pytest

port = pytest.importorskip("wreath.port")


def _analyze(tmp_path, source: str, name: str = "m.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return port.analyze(path).findings


def _by_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def _celery(findings):
    """Every Celery finding, whichever half of the split it landed on.

    The family is what these tests are about -- whether the *decorator* was seen
    at all -- and it is billed under `bg.celery.task` where the rewrite is
    determined and `bg.celery` where a keyword, a `self.retry()` or a sync
    caller still needs a person.
    """
    return [f for f in findings if f.rule_id.startswith("bg.celery")]


# --- boto3 is not one verdict ----------------------------------------------------


def test_an_s3_client_names_the_shipped_object_store(tmp_path) -> None:
    """`wreath.objects` shipped, so S3 stopped being "not a framework feature"."""
    findings = _analyze(tmp_path, 'import boto3\ns3 = boto3.client("s3")\n')
    (finding,) = _by_rule(findings, "ext.boto3_s3")
    assert finding.tag == port.NEEDS_REVIEW
    assert "S3ObjectStore" in finding.message
    assert not _by_rule(findings, "ext.boto3")


@pytest.mark.parametrize("service", ["dynamodb", "batch", "sqs"])
def test_another_aws_service_is_still_unsupported(tmp_path, service) -> None:
    """The split is by service, not by import — everything else keeps its verdict."""
    findings = _analyze(tmp_path, f'import boto3\nc = boto3.client("{service}")\n')
    (finding,) = _by_rule(findings, "ext.boto3")
    assert finding.tag == port.UNSUPPORTED
    assert not _by_rule(findings, "ext.boto3_s3")


def test_a_runtime_service_name_gets_the_conservative_verdict(tmp_path) -> None:
    """Guessing S3 from a variable would put a wrong target on the wrong call."""
    findings = _analyze(tmp_path, "import boto3\nc = boto3.client(name)\n")
    assert _by_rule(findings, "ext.boto3")
    assert not _by_rule(findings, "ext.boto3_s3")


def test_one_module_talking_to_both_bills_both(tmp_path) -> None:
    source = 'import boto3\na = boto3.client("s3")\nb = boto3.resource("dynamodb")\n'
    findings = _analyze(tmp_path, source)
    assert _by_rule(findings, "ext.boto3_s3") and _by_rule(findings, "ext.boto3")


# --- constructs that were invisible ----------------------------------------------


def test_a_middleware_subclass_is_reported_where_it_is_defined(tmp_path) -> None:
    """`mw.custom` fired only where one was *wired up*, never where it was written.

    A BaseHTTPMiddleware subclass in its own module and imported elsewhere is the
    ordinary layout, and it produced no finding at all — so the class a porter
    has to rewrite was missing from the worklist.
    """
    source = (
        "from starlette.middleware.base import BaseHTTPMiddleware\n"
        "\n"
        "class TrailState(BaseHTTPMiddleware):\n"
        "    async def dispatch(self, request, call_next):\n"
        "        return await call_next(request)\n"
    )
    (finding,) = _by_rule(_analyze(tmp_path, source), "mw.state")
    assert finding.tag == port.NEEDS_REVIEW
    assert finding.line == 3


def test_an_hmac_signature_verify_names_the_shipped_verifier(tmp_path) -> None:
    """The hand-rolled form compares the digest only; the shipped one does more."""
    source = (
        "import hashlib\n"
        "import hmac\n"
        "\n"
        "def verify(secret, body, signature):\n"
        "    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()\n"
        "    return hmac.compare_digest(expected, signature)\n"
    )
    (finding,) = _by_rule(_analyze(tmp_path, source), "webhook.hmac")
    assert finding.tag == port.NEEDS_REVIEW
    assert "HMACWebhookVerifier" in finding.message
    assert "replay" in finding.message


def test_the_two_halves_of_one_verify_bill_once(tmp_path) -> None:
    """`hmac.new` and the `compare_digest` reading it are one port, not two."""
    source = (
        "import hmac, hashlib\n"
        "a = hmac.new(b'k', b'b', hashlib.sha256).hexdigest()\n"
        "ok = hmac.compare_digest(a, 'sig')\n"
    )
    assert len(_by_rule(_analyze(tmp_path, source), "webhook.hmac")) == 1


# --- the deferred-migration target -----------------------------------------------


def test_a_data_migration_names_recode_not_alembic(tmp_path) -> None:
    """Deferred data migrations shipped, so "keep it in Alembic" stopped being true."""
    source = (
        "from alembic import op\n"
        "\n"
        "def upgrade():\n"
        "    conn = op.get_bind()\n"
        "    conn.execute('UPDATE treks SET grade = 1')\n"
    )
    (finding,) = _by_rule(_analyze(tmp_path, source), "mig.data")
    assert finding.tag == port.NEEDS_REVIEW
    assert "Recode" in finding.message
    assert "no online/deferred backfill" not in finding.message


# --- background workers the analyzer could not see -------------------------------


@pytest.mark.parametrize(
    ("decorator", "why"),
    [
        ("@celery_app.task(bind=True, max_retries=5)", "a Call decorator"),
        ("@celery_app.task", "a bare Attribute decorator"),
    ],
)
def test_both_spellings_of_a_celery_task_are_billed(tmp_path, decorator, why) -> None:
    """The bare form was invisible, and the emitter had always seen it.

    `emit.py` matches on the resolved tail, so `@celery_app.task` (no call) got a
    `# TODO(wreath-port: bg.celery)` in the ported source while the report said
    nothing about it. A porter grepping one and reading the other found two
    different answers for the same line -- the exact divergence `query_rule`'s
    docstring guards against, in a rule that does not go through `query_rule`.
    """
    source = (
        "from celery import Celery\n"
        "celery_app = Celery('x')\n"
        f"{decorator}\n"
        "def work() -> None:\n"
        "    pass\n"
    )
    (finding,) = _celery(_analyze(tmp_path, source))
    # Both spellings land on the *written-out* half of the split: neither
    # carries a keyword without a `JobRunner.task` equivalent, and neither body
    # calls `self.retry()`.
    assert finding.rule_id == "bg.celery.task", why
    assert finding.tag == port.TRANSLATED, why
    assert "JobRunner" in finding.message


@pytest.mark.parametrize("runner", ["relay", "app", "worker", "queue"])
def test_a_celery_app_is_recognized_by_its_binding_not_its_name(tmp_path, runner) -> None:
    """`@relay.task` was invisible because the gate read the *variable's* name.

    `origin()` resolves imports, and a Celery application is a local bound to a
    call, so the resolved origin of `@relay.task` is the literal text
    `relay.task` -- and the gate asked whether it contained "celery". Every app
    that did not name its runner after the library reported nothing at all,
    while the emitter (which tracks the assignment) rewrote the same decorator.
    The binding is the fact; the name is a coincidence.
    """
    source = (
        "from celery import Celery\n"
        f"{runner} = Celery('x')\n"
        f"@{runner}.task\n"
        "def work() -> None:\n"
        "    pass\n"
    )
    (finding,) = _celery(_analyze(tmp_path, source))
    assert finding.rule_id.startswith("bg.celery")
    assert finding.line == 3


def test_a_runner_built_by_a_factory_still_bills_its_tasks(tmp_path) -> None:
    """`worker = make_celery(app)` binds no `Celery(...)` this file can see.

    The factory spelling is what a Flask-era codebase carries into FastAPI, and
    the assignment says nothing. The module importing celery at all is the
    discriminator that is left -- the same loose, module-level question
    `serves_asgi` asks about a route decorator, and for the same reason.
    """
    source = (
        "from celery import Celery\n"
        "from .factory import make_celery\n"
        "worker = make_celery('x')\n"
        "@worker.task\n"
        "def work() -> None:\n"
        "    pass\n"
    )
    (finding,) = _celery(_analyze(tmp_path, source))
    assert finding.rule_id.startswith("bg.celery")


def test_a_task_decorator_in_a_module_with_no_celery_is_not_billed(tmp_path) -> None:
    """`@scheduler.task` from some other library must not become a Celery finding.

    The catch-all above is scoped to a module that imports celery. Without that
    scope it is not a catch-all, it is a match on the word "task".
    """
    source = (
        "from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
        "scheduler = AsyncIOScheduler()\n"
        "@scheduler.task\n"
        "def work() -> None:\n"
        "    pass\n"
    )
    assert not _celery(_analyze(tmp_path, source))


def test_a_multiprocessing_worker_names_jobs_and_progress(tmp_path) -> None:
    """`Process` + a polled state file is `jobs.launch()` + progress reports."""
    source = (
        "import multiprocessing\n"
        "def run(job_id): pass\n"
        "proc = multiprocessing.Process(target=run, args=('a',))\n"
        "proc.start()\n"
        "proc.join()\n"
    )
    (finding,) = _by_rule(_analyze(tmp_path, source), "bg.multiprocessing")
    assert finding.tag == port.NEEDS_REVIEW
    assert "jobs.launch" in finding.message
    assert "jobs.launch()" in finding.message
    assert "progress" in finding.message


def test_one_worker_is_one_finding_not_four(tmp_path) -> None:
    """`.start()`/`.join()`/`.is_alive()` are the same worker as the spawn."""
    source = (
        "import multiprocessing\n"
        "def run(): pass\n"
        "p = multiprocessing.Process(target=run)\n"
        "p.start()\n"
        "while p.is_alive():\n"
        "    pass\n"
        "p.join()\n"
    )
    assert len(_by_rule(_analyze(tmp_path, source), "bg.multiprocessing")) == 1
