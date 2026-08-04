"""Source-level security rules: `wreath audit code`.

Every rule is tested twice — once against the shape it is meant to catch, and
once against the correct spelling of the same intent, which must stay silent.
The second half is the one that matters: a rule that fires on the safe form is
worse than no rule, because a gate nobody can keep clean is a gate everybody
learns to pass with `--no-verify`.

Each vulnerable sample here is a reduction of a defect that was planted in a
red-team range and captured, so the shapes are the ones that actually ship
rather than the ones that are easy to match.
"""

from __future__ import annotations

import pytest

from wreath._audit.model import Severity
from wreath._audit.rules.code import CODE_RULES, scan_source


def ids_for(source: str) -> set[str]:
    return {finding.rule_id for finding in scan_source(source, surface="sample.py")}


def assert_flags(source: str, rule_id: str) -> None:
    found = ids_for(source)
    assert rule_id in found, f"expected {rule_id}, got {sorted(found) or 'nothing'}"


def assert_clean(source: str, rule_id: str) -> None:
    found = ids_for(source)
    assert rule_id not in found, f"{rule_id} fired on the safe form"


# --- sql-interpolation -------------------------------------------------------


def test_raw_sql_built_by_f_string_is_flagged() -> None:
    assert_flags(
        """
        @router.get("/search")
        async def search(request, session, q: str):
            return await session.raw(f"SELECT * FROM t WHERE name ILIKE '%{q}%'").fetch()
        """,
        "sql-interpolation",
    )


def test_raw_sql_built_by_concatenation_is_flagged() -> None:
    assert_flags(
        """
        @router.get("/search")
        async def search(request, session, q: str):
            return await session.raw("SELECT * FROM t WHERE name = '" + q + "'").fetch()
        """,
        "sql-interpolation",
    )


def test_raw_sql_built_by_percent_formatting_is_flagged() -> None:
    assert_flags(
        """
        @router.get("/search")
        async def search(request, session, q: str):
            return await session.raw("SELECT * FROM t WHERE name = '%s'" % q).fetch()
        """,
        "sql-interpolation",
    )


def test_interpolation_reaching_the_sink_through_a_variable_is_flagged() -> None:
    """The real shape: the caller's value is laundered through two locals first.

    Nothing about this is safer than inlining it, and an audit that only
    matched the inline form would miss every application that formats its SQL
    on the line above the one that runs it.
    """
    assert_flags(
        """
        @router.get("/search")
        async def search(request, session, q: str):
            needle = q.replace(";", "")
            sql = f"SELECT id FROM t WHERE name ILIKE '%{needle}%'"
            return await session.raw(sql).fetch()
        """,
        "sql-interpolation",
    )


def test_parameterised_raw_sql_is_clean() -> None:
    assert_clean(
        """
        @router.get("/search")
        async def search(request, session, q: str):
            return await session.raw("SELECT * FROM t WHERE name = $1", q).fetch()
        """,
        "sql-interpolation",
    )


def test_schema_qualified_sql_from_module_constants_is_clean() -> None:
    """Interpolating an identifier the application owns is how this is written.

    Wreath's own `_locks.py` does it in seven places and `example/` in six. A
    rule that cannot tell this from an injection reports 109 findings against
    correct code, which is how a security gate gets switched off.
    """
    assert_clean(
        """
        SCHEMA = "camera_trap"
        TABLES = ("sightings", "stations")

        async def truncate(connection):
            await connection.execute(
                "TRUNCATE " + ", ".join(f'"{SCHEMA}"."{t}"' for t in TABLES)
            )
        """,
        "sql-interpolation",
    )


def test_interpolated_sql_outside_a_handler_is_clean() -> None:
    """A seeding script, a migration, a CLI. No caller chose these values."""
    assert_clean(
        """
        async def seed(connection, table, columns):
            await connection.execute(f"INSERT INTO {table} ({columns}) VALUES ($1)", 1)
        """,
        "sql-interpolation",
    )


def test_f_string_with_no_interpolation_is_clean() -> None:
    """A constant f-string is a constant. Flagging it teaches nothing."""
    assert_clean(
        """
        async def search(session):
            return await session.raw(f"SELECT 1").fetch()
        """,
        "sql-interpolation",
    )


# --- timing-unsafe-compare ---------------------------------------------------


def test_equality_on_a_signature_is_flagged() -> None:
    assert_flags(
        """
        def check(given, expected_signature):
            return given == expected_signature
        """,
        "timing-unsafe-compare",
    )


def test_compare_digest_is_clean() -> None:
    assert_clean(
        """
        import hmac
        def check(given, expected_signature):
            return hmac.compare_digest(given, expected_signature)
        """,
        "timing-unsafe-compare",
    )


def test_equality_on_an_ordinary_name_is_clean() -> None:
    assert_clean(
        """
        def check(status, expected):
            return status == expected
        """,
        "timing-unsafe-compare",
    )


# --- weak-randomness ---------------------------------------------------------


def test_random_choice_for_a_token_is_flagged() -> None:
    assert_flags(
        """
        import random
        def issue(account_id):
            generator = random.Random(account_id)
            token = "".join(generator.choice("0123456789abcdef") for _ in range(32))
            return token
        """,
        "weak-randomness",
    )


def test_secrets_module_is_clean() -> None:
    assert_clean(
        """
        import secrets
        def issue():
            token = secrets.token_urlsafe(32)
            return token
        """,
        "weak-randomness",
    )


def test_random_for_a_non_security_value_is_clean() -> None:
    assert_clean(
        """
        import random
        def jitter(base):
            delay = base * random.random()
            return delay
        """,
        "weak-randomness",
    )


# --- hardcoded-secret --------------------------------------------------------


def test_literal_session_secret_is_flagged() -> None:
    assert_flags(
        """
        from wreath.middleware import SessionMiddleware
        app.add_global_middleware(SessionMiddleware("northwind-dev-secret"))
        """,
        "hardcoded-secret",
    )


def test_secret_read_from_the_environment_is_clean() -> None:
    assert_clean(
        """
        import os
        from wreath.middleware import SessionMiddleware
        app.add_global_middleware(SessionMiddleware(os.environ["SESSION_SECRET"]))
        """,
        "hardcoded-secret",
    )


# --- ssrf-policy-widened -----------------------------------------------------


def test_loopback_permitted_on_a_destination_policy_is_flagged() -> None:
    assert_flags(
        """
        from wreath.http_client import DestinationPolicy, HTTPClient
        client = HTTPClient("x", base_url=url, destination=DestinationPolicy(allow_loopback=True))
        """,
        "ssrf-policy-widened",
    )


def test_default_destination_policy_is_clean() -> None:
    assert_clean(
        """
        from wreath.http_client import DestinationPolicy, HTTPClient
        client = HTTPClient("x", base_url=url,
                            destination=DestinationPolicy(hosts=("api.partner.example",)))
        """,
        "ssrf-policy-widened",
    )


# --- unsafe-xml-parser -------------------------------------------------------


def test_external_entities_enabled_is_flagged() -> None:
    assert_flags(
        """
        import xml.sax
        parser = xml.sax.make_parser()
        parser.setFeature(xml.sax.handler.feature_external_ges, True)
        """,
        "unsafe-xml-parser",
    )


def test_stdlib_elementtree_parse_is_flagged() -> None:
    assert_flags(
        """
        import xml.etree.ElementTree as ET
        def load(body):
            return ET.fromstring(body)
        """,
        "unsafe-xml-parser",
    )


def test_wreath_xml_parse_is_clean() -> None:
    assert_clean(
        """
        from wreath.xml import parse
        def load(body):
            return parse(body)
        """,
        "unsafe-xml-parser",
    )


# --- template-from-request ---------------------------------------------------


def test_template_compiled_from_a_variable_is_flagged() -> None:
    assert_flags(
        """
        from wreath.templates import Template
        def render(source, context):
            return Template.from_string(source).render(**context)
        """,
        "template-from-request",
    )


def test_template_compiled_from_a_literal_is_clean() -> None:
    assert_clean(
        """
        from wreath.templates import Template
        GREETING = Template.from_string("Hello {{ name }}")
        """,
        "template-from-request",
    )


# --- dynamic-import ----------------------------------------------------------


def test_import_module_from_a_variable_is_flagged() -> None:
    assert_flags(
        """
        import importlib

        @router.post("/automation/run")
        async def run(request, action: str):
            module = importlib.import_module(action)
            return module
        """,
        "dynamic-import",
    )


def test_import_module_from_a_local_name_outside_a_handler_is_clean() -> None:
    """Loading an application by name is what a CLI does. It is not RCE."""
    assert_clean(
        """
        import importlib
        def load(target):
            module_name, _, attribute = target.partition(":")
            return getattr(importlib.import_module(module_name), attribute)
        """,
        "dynamic-import",
    )


def test_eval_is_flagged() -> None:
    assert_flags(
        """
        def run(expression):
            return eval(expression)
        """,
        "dynamic-import",
    )


def test_import_module_from_a_literal_is_clean() -> None:
    assert_clean(
        """
        import importlib
        backend = importlib.import_module("myapp.backends.postgres")
        """,
        "dynamic-import",
    )


# --- unsafe-archive-extract --------------------------------------------------


def test_zipfile_extractall_is_flagged() -> None:
    assert_flags(
        """
        import zipfile
        def unpack(blob, destination):
            with zipfile.ZipFile(blob) as archive:
                archive.extractall(destination)
        """,
        "unsafe-archive-extract",
    )


def test_tarfile_extractall_is_flagged() -> None:
    assert_flags(
        """
        import tarfile
        def unpack(blob, destination):
            tarfile.open(blob).extractall(destination)
        """,
        "unsafe-archive-extract",
    )


def test_wreath_unzip_stream_is_clean() -> None:
    assert_clean(
        """
        from wreath.objects import ZipExtractionLimits, unzip_stream
        async def unpack(chunks, store):
            await unzip_stream(chunks, store, limits=ZipExtractionLimits())
        """,
        "unsafe-archive-extract",
    )


# --- mass-assignment ---------------------------------------------------------


def test_setattr_loop_over_a_request_body_is_flagged() -> None:
    assert_flags(
        """
        async def patch(request, row):
            payload = await request.json()
            for key, value in payload.items():
                setattr(row, key, value)
        """,
        "mass-assignment",
    )


def test_explicit_field_assignment_is_clean() -> None:
    assert_clean(
        """
        async def patch(request, row):
            payload = await request.json()
            row.display_name = payload["display_name"]
        """,
        "mass-assignment",
    )


# --- case-mapped-authz -------------------------------------------------------


def test_uppercased_membership_test_against_an_allowlist_is_flagged() -> None:
    assert_flags(
        """
        OPS_ALLOWLIST = {"OPS@EXAMPLE.COM"}
        def is_staff(email):
            return email.upper() in OPS_ALLOWLIST
        """,
        "case-mapped-authz",
    )


def test_membership_test_without_case_mapping_is_clean() -> None:
    assert_clean(
        """
        OPS_ALLOWLIST = {"ops@example.com"}
        def is_staff(email):
            return email in OPS_ALLOWLIST
        """,
        "case-mapped-authz",
    )


# --- cors-reflect-origin -----------------------------------------------------


def test_origin_reflected_into_the_cors_header_is_flagged() -> None:
    assert_flags(
        """
        def after(request, response):
            origin = request.header("origin")
            response.headers.append((b"access-control-allow-origin", origin.encode()))
        """,
        "cors-reflect-origin",
    )


def test_explicit_cors_origins_are_clean() -> None:
    assert_clean(
        """
        from wreath.middleware import CORSMiddleware
        app.add_global_middleware(
            CORSMiddleware(allow_origins=("https://console.example",), allow_credentials=True)
        )
        """,
        "cors-reflect-origin",
    )


# --- debug-enabled -----------------------------------------------------------


def test_debug_true_on_the_application_is_flagged() -> None:
    assert_flags(
        """
        from wreath import Wreath
        app = Wreath(debug=True)
        """,
        "debug-enabled",
    )


def test_debug_from_configuration_is_clean() -> None:
    assert_clean(
        """
        import os
        from wreath import Wreath
        app = Wreath(debug=os.environ.get("DEBUG") == "1")
        """,
        "debug-enabled",
    )


# --- untrusted-forwarded-header ----------------------------------------------


def test_rate_limit_key_from_a_forwarded_header_is_flagged() -> None:
    assert_flags(
        """
        from wreath.middleware import RateLimitMiddleware
        def key(request):
            return request.header("x-forwarded-for")
        app.add_global_middleware(RateLimitMiddleware(limit=5, key=key))
        """,
        "untrusted-forwarded-header",
    )


def test_forwarded_header_with_proxy_middleware_configured_is_clean() -> None:
    """`ProxyHeadersMiddleware` is what makes the header mean something.

    The rule is about an *unestablished* header, so an application that
    configures the trust boundary is doing the right thing and must not be
    nagged for it.
    """
    assert_clean(
        """
        from wreath.middleware import ProxyHeadersMiddleware, RateLimitMiddleware
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=("10.0.0.1",)))
        def key(request):
            return request.header("x-forwarded-for")
        app.add_global_middleware(RateLimitMiddleware(limit=5, key=key))
        """,
        "untrusted-forwarded-header",
    )


# --- the rule set as a whole -------------------------------------------------


def test_every_rule_has_a_reference_and_a_suggestion() -> None:
    """A finding with no remediation is a complaint.

    Checked over the corpus rather than by inspecting the rule table, so a rule
    that forgets one on a particular branch is caught.
    """
    corpus = """
        import importlib, random, tarfile, xml.sax, zipfile
        from wreath import Wreath
        from wreath.http_client import DestinationPolicy
        from wreath.middleware import SessionMiddleware
        from wreath.templates import Template

        app = Wreath(debug=True)
        app.add_global_middleware(SessionMiddleware("dev-secret"))
        policy = DestinationPolicy(allow_loopback=True)
        token = random.Random(1).choice("abc")

        async def handler(request, session, row, q, source, action):
            await session.raw(f"SELECT {q}").fetch()
            payload = await request.json()
            for key, value in payload.items():
                setattr(row, key, value)
            Template.from_string(source)
            importlib.import_module(action)
            with zipfile.ZipFile(payload) as archive:
                archive.extractall("/tmp")
    """
    findings = scan_source(corpus, surface="corpus.py")
    assert findings, "the corpus should produce findings"
    for finding in findings:
        assert finding.reference, f"{finding.rule_id} has no reference"
        assert finding.suggestion, f"{finding.rule_id} has no suggestion"
        assert finding.location, f"{finding.rule_id} has no location"
        assert finding.severity in tuple(Severity)


def test_rule_ids_are_unique_and_kebab_case() -> None:
    ids = [rule.rule_id for rule in CODE_RULES]
    assert len(ids) == len(set(ids)), "duplicate rule id"
    for rule_id in ids:
        assert rule_id == rule_id.lower()
        assert " " not in rule_id and "_" not in rule_id


def test_clean_application_source_produces_nothing() -> None:
    """The whole point. A correct application is silent."""
    findings = scan_source(
        """
        import os
        from typing import Annotated

        from wreath import Request, Router
        from wreath.auth import authenticated
        from wreath.middleware import SessionMiddleware
        from wreath.orm import FromORM, Session

        ReadSession = Annotated[Session, FromORM("main", workload="read")]
        router = Router(prefix="/invoices")

        @router.get("/{invoice_id}")
        @authenticated()
        async def read_invoice(request: Request, db: ReadSession, invoice_id: int) -> dict:
            row = await db.fetch_one(
                Invoice.select().where(Invoice.id == invoice_id).where(
                    Invoice.org_id == request.identity.claims["org_id"]
                )
            )
            return {"id": row.id, "number": row.number}
        """,
        surface="clean.py",
    )
    assert findings == [], [f.rule_id for f in findings]


def test_syntax_error_is_reported_rather_than_raised() -> None:
    """An unparseable file must not abort a scan of a whole tree."""
    findings = scan_source("def broken(:\n", surface="broken.py")
    assert [f.rule_id for f in findings] == ["unparseable"]


@pytest.mark.parametrize("rule", CODE_RULES, ids=lambda rule: rule.rule_id)
def test_rule_documents_its_own_reference(rule) -> None:
    assert rule.reference, f"{rule.rule_id} declares no CWE or wreath reference"


# --- regressions from sweeping Wreath's own source ---------------------------
#
# Every test below is a false positive the first draft produced against
# `src/wreath`, reduced to its shape. Twenty-nine findings, of which
# twenty-eight were the detector's fault; keeping them as tests is what stops
# the next widening of a vocabulary from bringing them all back.


def test_attribute_assignment_does_not_taint_the_object() -> None:
    """`self.x = <request data>` must not make `self` caller-controlled.

    Target names were collected with `ast.walk`, so an attribute target
    contributed its *base*. Every `f"...{self.table}..."` in `webhooks.py` then
    looked like an injection -- nine of the twenty-nine.
    """
    assert_clean(
        """
        class Inbox:
            def __init__(self, table):
                self.table = table

            @router.post("/hook")
            async def receive(self, request, session):
                body = await request.body()
                self.last = body
                return await session.raw(
                    f"SELECT state FROM {self.table} WHERE id=$1", 1
                ).fetchrow()
        """,
        "sql-interpolation",
    )


def test_route_signature_is_not_a_cryptographic_signature() -> None:
    """`signature`, `token`, `digest` name non-secret things constantly.

    A route signature, a lexer token, a plan digest. Six false positives shared
    this shape, so the weak half of the vocabulary now needs a second signal:
    the other operand has to name a comparison role.
    """
    for source in (
        "def f(other, signature):\n    return other == signature\n",
        "def f(where, signature):\n    return _normalise(where) == signature\n",
        "def f(digest, plan):\n    return digest != plan.digest\n",
        "def f(token, kind):\n    return token[0] != kind\n",
    ):
        assert_clean(source, "timing-unsafe-compare")


def test_a_secret_compared_against_a_named_expectation_still_fires() -> None:
    """The recall half of the rule above: the a08 shape must survive it."""
    assert_flags(
        "def f(given, expected_signature):\n    return given == expected_signature\n",
        "timing-unsafe-compare",
    )


def test_identifier_suffixes_are_not_secrets() -> None:
    """`credential_id` is an identifier. `_TOKEN_VERSION` is a version."""
    for source in (
        "def f(rows, credential_id):\n    return [r for r in rows if r.id != credential_id]\n",
        "def f(parts):\n    return parts[0] != _TOKEN_VERSION\n",
        "def f(model, credential_id):\n    return model.select().where(model.id == credential_id)\n",
    ):
        assert_clean(source, "timing-unsafe-compare")


def test_template_compiled_from_a_module_constant_is_clean() -> None:
    """`Template.from_string(_SHELL)` at import time is the documented shape."""
    assert_clean(
        """
        _DOCS_SHELL = "<html>{{ title }}</html>"
        def build(title):
            return Template.from_string(_DOCS_SHELL).render(title=title)
        """,
        "template-from-request",
    )


def test_method_allowlist_is_not_an_identity_comparison() -> None:
    """CORS preflight uppercases an HTTP method. Methods are not principals."""
    assert_clean(
        """
        class CORS:
            def preflight(self, requested):
                if requested.upper() not in self._allow_methods:
                    return None
        """,
        "case-mapped-authz",
    )


def test_a_reviewed_waiver_is_honoured() -> None:
    """Code generation is what an ORM does, and Wreath's already carries the
    reviewed `# noqa: S102` that ruff's own rule asks for.

    Re-reporting something the project has already declared and justified is
    how a second tool gets switched off. The audit honours the existing
    directive rather than inventing a parallel one.
    """
    assert_clean(
        """
        def compile_extractor(body, namespace):
            exec(  # noqa: S102 - every fragment is identifier-checked above
                f"def extract(select):\\n    return ({body})", {}, namespace
            )
        """,
        "dynamic-import",
    )


def test_the_audits_own_waiver_marker_is_honoured() -> None:
    assert_clean(
        """
        import zipfile
        def unpack(blob, dest):
            # wreath-audit: allow unsafe-archive-extract -- trusted build artefact
            zipfile.ZipFile(blob).extractall(dest)
        """,
        "unsafe-archive-extract",
    )


def test_an_unwaived_exec_still_fires() -> None:
    assert_flags("def f(x):\n    exec(x)\n", "dynamic-import")


def test_a_deterministic_seeder_is_clean() -> None:
    """`random.Random(SEED)` with a module constant is reproducibility, not a
    security draw. Every data seeder and property test does it, and both of the
    example application's findings were this shape."""
    assert_clean(
        """
        SEED = 20260727
        def build_rows():
            rng = random.Random(SEED)
            return [rng.random() for _ in range(10)]
        """,
        "weak-randomness",
    )


def test_a_prng_seeded_from_a_caller_value_still_fires() -> None:
    """The a07 shape: seeded from something an attacker can enumerate."""
    assert_flags(
        """
        @router.post("/reset")
        async def reset(request, account_id: int):
            generator = random.Random(account_id)
            return "".join(generator.choice("0123456789abcdef") for _ in range(32))
        """,
        "weak-randomness",
    )
