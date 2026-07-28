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
    (finding,) = _by_rule(_analyze(tmp_path, source), "mw.custom")
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
    (finding,) = _by_rule(_analyze(tmp_path, source), "bg.celery")
    assert finding.tag == port.NEEDS_REVIEW, why
    assert "jobs" in finding.message


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
