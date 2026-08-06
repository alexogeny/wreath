"""Source-level security rules: `wreath audit code`.

Every rule is tested twice — once against the shape it is meant to catch, and
once against the correct spelling of the same intent, which must stay silent.
The second half is the one that matters: a rule that fires on the safe form is
worse than no rule, because a gate nobody can keep clean is a gate everybody
learns to pass with `--no-verify`.

Each vulnerable sample is a reduction of a defect class that ships in real
applications, so the shapes are the ones that actually get written rather than
the ones that are easy to match.

Three kinds of test live here, and the order matters:

1. the shape a rule catches;
2. the correct spelling of the same intent, which must stay silent;
3. and, at the bottom, the tests that hold a *precision guard* down.

The third kind exists because the second is not self-verifying. `assert_clean`
passes against a rule that never fires at all, so a quiet test nobody has
falsified is not evidence -- and `wreath mutant --changed` proved it, removing
guard after guard with the whole file still green. Every test in that last
section names the guard it holds and was written because a mutant survived
without it.
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
        "def f(model, credential_id):\n"
        "    return model.select().where(model.id == credential_id)\n",
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


# --- wildcard-trust-list, and the hole it exposed ----------------------------


def test_wildcard_host_allowlist_is_flagged() -> None:
    assert_flags(
        """
        from wreath.middleware import TrustedHostMiddleware
        app.add_global_middleware(TrustedHostMiddleware(allowed_hosts=["*"]))
        """,
        "wildcard-trust-list",
    )


def test_wildcard_cors_origins_are_flagged() -> None:
    assert_flags(
        """
        from wreath.middleware import CORSMiddleware
        app.add_global_middleware(
            CORSMiddleware(allow_origins=["*"], allow_credentials=True)
        )
        """,
        "wildcard-trust-list",
    )


def test_real_trust_lists_are_clean() -> None:
    assert_clean(
        """
        from wreath.middleware import CORSMiddleware, ProxyHeadersMiddleware
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]))
        app.add_global_middleware(
            CORSMiddleware(allow_origins=["https://console.example"])
        )
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_proxy_boundary_does_not_silence_the_forwarded_rule() -> None:
    """The worst spelling must not buy the quietest result.

    `prepare` used to set `proxy_trusted` on *any* `ProxyHeadersMiddleware(...)`
    call. An application that wrote `trusted=["*"]` -- a boundary that trusts
    every peer, which is to say no boundary -- therefore silenced
    `untrusted-forwarded-header` for the whole file. The audit rewarded the one
    configuration that deserved it least.
    """
    source = """
        from wreath.middleware import ProxyHeadersMiddleware, RateLimitMiddleware
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=["*"]))
        def key(request):
            return request.header("x-forwarded-for")
        app.add_global_middleware(RateLimitMiddleware(limit=5, key=key))
    """
    assert_flags(source, "untrusted-forwarded-header")
    assert_flags(source, "wildcard-trust-list")


# =============================================================================
# Taint and security-smell rules
# =============================================================================
#
# The tier's second half. The rules above ask "is this expression dangerous?";
# these ask two further questions that the same AST walk can answer and that no
# expression-level rule can:
#
#   * What does this *declaration* say?  A signing key is most often a default
#     value on a settings field, not an argument to a call.
#   * What happens on the branch where the check could not be made?  A control
#     that raises on denial and *returns* on "cannot tell" has two exits and one
#     of them is open.
#
# Same contract as everything above: each rule is tested against the shape it
# catches and against the correct spelling of the same intent.


# --- hardcoded-secret, declared rather than passed ---------------------------


def test_a_signing_key_declared_as_a_settings_default_is_flagged() -> None:
    """The shape a signing key actually ships in.

    Not an argument to `SessionMiddleware` -- a default on a settings field, so
    that every deployment which has not set the environment variable signs with
    the key that is in the source. Nothing at startup complains, because from
    the application's point of view the setting is populated.
    """
    assert_flags(
        """
        class Settings(BaseSettings):
            SESSION_SECRET: str = "3f8a1c9e42b7d05f6a1e8c33b90d47e25fa6c018b4d97e3a"
            HASH_ALGORITHM: str = "HS256"
        """,
        "hardcoded-secret",
    )


def test_a_module_level_secret_assignment_is_flagged() -> None:
    assert_flags(
        """
        SIGNING_KEY = "8c1f0a77e5b3d942ac6e10bf35d8724e"
        """,
        "hardcoded-secret",
    )


def test_a_short_declared_constant_is_not_a_secret() -> None:
    """An algorithm name, a cookie name, a scheme. Short literals under a
    secret-ish name are format markers, and flagging them is how a rule that
    matters gets switched off for the ones that do not."""
    for source in (
        'HASH_ALGORITHM: str = "HS256"\n',
        'TOKEN_PREFIX = "Bearer"\n',
        'SECRET_HEADER = "x-trek-key"\n',
    ):
        assert_clean(source, "hardcoded-secret")


def test_a_secret_read_from_the_environment_is_clean() -> None:
    assert_clean(
        """
        import os
        SIGNING_KEY = os.environ["TREK_SIGNING_KEY"]
        """,
        "hardcoded-secret",
    )


def test_an_empty_declared_secret_is_clean() -> None:
    """`SESSION_SECRET: str = ""` is a required setting with no default."""
    assert_clean('SESSION_SECRET: str = ""\n', "hardcoded-secret")


# --- wildcard-trust-list -----------------------------------------------------


def test_a_wildcard_host_allow_list_is_flagged() -> None:
    assert_flags(
        """
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_proxy_trust_is_flagged() -> None:
    assert_flags(
        """
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=["*"]))
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_origin_with_credentials_is_flagged() -> None:
    assert_flags(
        """
        app.add_global_middleware(
            CORSMiddleware(allow_origins=["*"], allow_credentials=True)
        )
        """,
        "wildcard-trust-list",
    )


def test_a_named_trust_list_is_clean() -> None:
    assert_clean(
        """
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]))
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["trek.example"])
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_origin_without_credentials_is_clean() -> None:
    """A public read-only API is allowed to answer any origin. It is the
    *combination* with credentials that makes the wildcard a trust decision,
    which is why `CORSMiddleware` refuses only that pair."""
    assert_clean(
        """
        app.add_global_middleware(CORSMiddleware(allow_origins=["*"]))
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_proxy_trust_does_not_establish_the_boundary() -> None:
    """The regression this rule exists for.

    `untrusted-forwarded-header` is silenced by the presence of a proxy trust
    boundary. A trust list of `*` is not a boundary -- it trusts every peer,
    which is the state the rule is about -- so the forwarded-header finding must
    survive it.
    """
    assert_flags(
        """
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=["*"]))

        @router.get("/sightings")
        async def sightings(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_a_configured_proxy_trust_still_silences_the_forwarded_rule() -> None:
    """The other half: a real boundary must keep working."""
    assert_clean(
        """
        app.add_global_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]))

        @router.get("/sightings")
        async def sightings(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


# --- timing-unsafe-compare, paired by provenance -----------------------------


def test_a_double_submit_token_compared_with_equality_is_flagged() -> None:
    """Neither operand names a comparison role, so the `expected`/`provided`
    signal does not fire -- but the pair carries the same signal in a different
    spelling. Two secret-named values whose *provenance* differs are being
    checked against each other, and that is the comparison this rule is about.
    """
    assert_flags(
        """
        def check(request):
            cookie_token = request.cookies.get("csrf")
            header_token = request.headers.get("x-csrf-token")
            return cookie_token == header_token
        """,
        "timing-unsafe-compare",
    )


def test_two_tokens_of_the_same_provenance_are_clean() -> None:
    """`header_token != header_prefix` is one value being taken apart, not a
    secret being checked against a presented one."""
    assert_clean(
        """
        def check(header_token, header_scheme):
            return header_token != header_scheme
        """,
        "timing-unsafe-compare",
    )


# --- secret-in-log -----------------------------------------------------------


def test_a_credential_interpolated_into_a_log_line_is_flagged() -> None:
    """Debug logging is off in production until the day somebody turns it on to
    investigate an incident, which is the day the credential leaves the
    database's retention policy for the log aggregator's."""
    assert_flags(
        """
        @router.post("/stations/enroll")
        async def enroll(request, credentials: StationCredentials):
            logger.debug(f"enrolling with {credentials.as_dict()}")
        """,
        "secret-in-log",
    )


def test_a_percent_formatted_secret_is_flagged() -> None:
    assert_flags(
        """
        def verify(presented_password):
            logger.info("presented password `%s`" % presented_password)
        """,
        "secret-in-log",
    )


def test_a_secret_passed_as_a_logging_argument_is_flagged() -> None:
    """`logger.info("...%s", value)` defers the formatting; the value still
    reaches the record."""
    assert_flags(
        """
        def verify(api_key):
            logger.warning("rejecting api key %s", api_key)
        """,
        "secret-in-log",
    )


def test_a_logged_request_body_is_flagged() -> None:
    """The other half of the rule: a body the caller supplied is not known to
    be free of credentials, and this is the shape a catch-all error handler
    reaches for when it wants context."""
    assert_flags(
        """
        @router.post("/sightings")
        async def ingest(request):
            body = await request.json()
            try:
                await store(body)
            except StorageError:
                logger.error(f"could not store sighting: {body}")
        """,
        "secret-in-log",
    )


def test_logging_an_identifier_is_clean() -> None:
    for source in (
        'def f(station_id):\n    logger.info(f"enrolled station {station_id}")\n',
        'def f(token_id):\n    logger.info("issued token %s", token_id)\n',
        'def f(credential_id):\n    logger.debug(f"revoked {credential_id}")\n',
    ):
        assert_clean(source, "secret-in-log")


def test_mentioning_a_secret_without_interpolating_it_is_clean() -> None:
    """The name in the message is a label, not a value."""
    assert_clean(
        """
        def verify(token):
            logger.warning("the presented token was rejected")
        """,
        "secret-in-log",
    )


def test_a_secret_outside_a_logging_call_is_clean() -> None:
    """This rule is about the sink. Formatting a token into a request header is
    what a client is for."""
    assert_clean(
        """
        def call(session_token):
            return {"authorization": f"Bearer {session_token}"}
        """,
        "secret-in-log",
    )


# --- authz-fail-open ---------------------------------------------------------


def test_an_authorization_check_that_returns_when_undecided_is_flagged() -> None:
    """The defect is not the `return`. It is that the undecidable case and the
    permitted case are spelled identically, so a caller cannot distinguish
    "allowed" from "could not tell" -- and the comment on that branch is always
    some version of "the handler will sort it out"."""
    assert_flags(
        """
        async def authorise_reserve(request):
            principal = request.state.principal
            if not principal:
                raise Forbidden("no principal")

            reserve_id = resolve_reserve(request.path_params, request.query_params)
            if reserve_id is None:
                # let the handler decide
                return

            if reserve_id not in principal.reserves:
                raise Forbidden("not a member of this reserve")
            return principal.id
        """,
        "authz-fail-open",
    )


def test_an_authorization_check_returning_an_empty_scope_is_flagged() -> None:
    """The same exit with a different spelling: an empty filter is not a
    restriction, it is every row."""
    assert_flags(
        """
        def reserve_filter(request, context):
            if not context.get("reserve"):
                return {}
            if context["reserve"] not in request.state.principal.reserves:
                raise Forbidden("not a member of this reserve")
            return {"reserve": context["reserve"]}
        """,
        "authz-fail-open",
    )


def test_an_authorization_check_that_raises_when_undecided_is_clean() -> None:
    assert_clean(
        """
        async def authorise_reserve(request):
            reserve_id = resolve_reserve(request.path_params)
            if reserve_id is None:
                raise Forbidden("could not resolve the reserve")
            if reserve_id not in request.state.principal.reserves:
                raise Forbidden("not a member of this reserve")
            return request.state.principal.id
        """,
        "authz-fail-open",
    )


def test_an_ordinary_lookup_that_returns_none_is_clean() -> None:
    """A function that does not decide authorization has nothing to fail open.
    This is the precondition that keeps the rule off every `get_or_none`."""
    assert_clean(
        """
        async def find_station(session, station_id):
            if station_id is None:
                return None
            return await session.fetch_one(Station.select().where(Station.id == station_id))
        """,
        "authz-fail-open",
    )


def test_returning_a_decision_from_an_authorization_check_is_clean() -> None:
    """`return principal.id` on the permitted branch is the success path."""
    assert_clean(
        """
        async def authorise_reserve(request, reserve_id):
            if reserve_id in request.state.principal.reserves:
                return request.state.principal.id
            raise Forbidden("not a member of this reserve")
        """,
        "authz-fail-open",
    )


# --- auth-disable-flag -------------------------------------------------------


def test_a_configuration_flag_that_skips_authentication_is_flagged() -> None:
    """A backdoor with a config key attached, one environment variable away
    from production every day of its life."""
    assert_flags(
        """
        class VerifyToken:
            async def __call__(self, authorization):
                if settings.NO_AUTH:
                    logger.debug("authentication skipped")
                    return True
                return await verify(authorization)
        """,
        "auth-disable-flag",
    )


def test_a_ternary_that_drops_the_security_dependency_is_flagged() -> None:
    assert_flags(
        """
        scheme = api_key_header if not settings.SKIP_AUTH else ""
        """,
        "auth-disable-flag",
    )


def test_a_flag_that_enables_a_control_is_clean() -> None:
    """The vocabulary is deliberately the disabling half only. A flag that
    turns a control *on* is configuration, not a bypass."""
    for source in (
        "if settings.REQUIRE_MFA:\n    enforce_mfa(principal)\n",
        "if not settings.TELEMETRY_ENABLED:\n    return\n",
        "limit = 5 if settings.THROTTLE_ENABLED else 500\n",
    ):
        assert_clean(source, "auth-disable-flag")


# --- error-detail-leaked -----------------------------------------------------


def test_a_caught_exception_returned_as_the_detail_is_flagged() -> None:
    """The driver's message names the schema, the statement, sometimes the
    parameters -- written for one debugging session and never removed."""
    assert_flags(
        """
        @router.post("/sightings")
        async def ingest(request, session):
            try:
                return await store(session, request)
            except Exception as error:
                raise InternalError(detail=str(error))
        """,
        "error-detail-leaked",
    )


def test_a_caught_exception_interpolated_into_a_response_is_flagged() -> None:
    assert_flags(
        """
        def render(reserve_id):
            try:
                return load(reserve_id)
            except Exception as error:
                return JSONResponse({"detail": f"could not load: {error}"}, status_code=500)
        """,
        "error-detail-leaked",
    )


def test_a_named_exceptions_message_is_clean() -> None:
    """The precision half, and it is not a concession.

    If you named the type you caught, you know what its message says because
    you wrote it -- a refusal addressed to the caller is the *point* of a
    self-authored exception, and `AGENTS.md` asks for exactly this narrow catch
    in preference to a broad one. Six places in Wreath's own source are this
    shape. It is the broad handler that cannot know what it is holding.
    """
    assert_clean(
        """
        def decode(body):
            try:
                return _protobuf_decode(body)
            except _ProtobufDecodeError as exc:
                raise BadRequest(f"invalid protobuf body: {exc}") from None
        """,
        "error-detail-leaked",
    )


def test_a_written_refusal_message_is_clean() -> None:
    assert_clean(
        """
        def render(reserve_id):
            try:
                return load(reserve_id)
            except Exception as error:
                log.error("reserve load failed", reserve=reserve_id)
                raise UnprocessableEntity("the reserve could not be read") from error
        """,
        "error-detail-leaked",
    )


def test_logging_a_caught_exception_is_clean() -> None:
    """A log is not a response. This rule is about what reaches the caller."""
    assert_clean(
        """
        def render(reserve_id):
            try:
                return load(reserve_id)
            except Exception as error:
                logger.error("reserve load failed: %s", error)
                raise UnprocessableEntity("the reserve could not be read") from error
        """,
        "error-detail-leaked",
    )


def test_a_result_object_carrying_an_error_field_is_clean() -> None:
    """`PushResult(error=str(exc))` is a value the caller of a *function* reads,
    not a body the caller of a *route* receives. Treating every `error=` keyword
    as a response accounted for ten of this rule's first sixteen findings."""
    assert_clean(
        """
        def deliver(message):
            try:
                return PushResult(delivered=True)
            except Exception as error:
                return PushResult(delivered=False, error=str(error))
        """,
        "error-detail-leaked",
    )


# --- outbound-url-from-request -----------------------------------------------


def test_a_fetch_of_a_caller_supplied_url_is_flagged() -> None:
    """The pivot to a metadata endpoint. Validating the string once is not a
    fix: the redirect target and every DNS answer are the destination too."""
    assert_flags(
        """
        @router.post("/stations/register")
        async def register(request, registration: StationRegistration):
            async with httpx.AsyncClient() as client:
                return await client.get(registration.callback_url)
        """,
        "outbound-url-from-request",
    )


def test_a_fetch_of_a_url_passed_by_keyword_is_flagged() -> None:
    assert_flags(
        """
        @router.post("/stations/register")
        async def register(request, callback: str):
            return await http_client.request("GET", url=callback)
        """,
        "outbound-url-from-request",
    )


def test_a_fetch_behind_a_destination_policy_is_clean() -> None:
    assert_clean(
        """
        @router.post("/stations/register")
        async def register(request, registration: StationRegistration):
            client = HTTPClient(policy=DestinationPolicy(hosts=("*.reserve.example",)))
            return await client.get(registration.callback_url)
        """,
        "outbound-url-from-request",
    )


def test_a_fetch_of_a_configured_url_is_clean() -> None:
    assert_clean(
        """
        UPSTREAM = "https://sightings.reserve.example/ingest"

        @router.post("/stations/register")
        async def register(request, client, station_id: str):
            return await client.get(UPSTREAM)
        """,
        "outbound-url-from-request",
    )


def test_a_mapping_lookup_on_request_data_is_clean() -> None:
    """`.get` is `dict.get` far more often than it is an HTTP verb, and the
    caller's own body is the most common receiver. Requiring the receiver to
    name a client is what keeps this rule off every handler."""
    assert_clean(
        """
        @router.post("/sightings")
        async def ingest(request):
            body = await request.json()
            return body.get("station")
        """,
        "outbound-url-from-request",
    )


# --- env-conditional-security ------------------------------------------------


def test_a_cookie_flag_conditioned_on_the_environment_is_flagged() -> None:
    """Every review reads "secure in production" and stops. Nobody rereads it
    when a new environment name appears, or when the variable is unset -- and
    the unset default is the insecure arm."""
    assert_flags(
        """
        ENV = os.getenv("ENV", "develop")

        class SessionCookie:
            secure: bool = True if ENV == "production" else False
            httponly: bool = ENV not in ("develop", "test")
        """,
        "env-conditional-security",
    )


def test_a_csrf_switch_conditioned_on_the_environment_is_flagged() -> None:
    assert_flags(
        """
        ENV = os.getenv("ENV", "develop")
        csrf_required = ENV == "production"
        """,
        "env-conditional-security",
    )


def test_a_non_security_setting_conditioned_on_the_environment_is_clean() -> None:
    """Environments differ. That is what they are for -- the rule is about the
    narrow set of values whose weak side is a vulnerability."""
    for source in (
        'PAGE_SIZE = 100 if ENV == "production" else 5\n',
        'DEBUG_TOOLBAR = ENV == "develop"\n',
        'LOG_LEVEL = "INFO" if ENV == "production" else "DEBUG"\n',
    ):
        assert_clean(source, "env-conditional-security")


def test_a_secure_default_is_clean() -> None:
    assert_clean(
        """
        class SessionCookie:
            secure: bool = True
            httponly: bool = True
            samesite: str = "strict"
        """,
        "env-conditional-security",
    )


# --- substring-security-match ------------------------------------------------


def test_a_path_exemption_matched_by_substring_is_flagged() -> None:
    """Directly exploitable: register anything whose path merely *contains* an
    exempted segment and the control is off for it."""
    assert_flags(
        """
        def is_exempt(path: str) -> bool:
            return any(route in path for route in EXEMPT_ROUTES)
        """,
        "substring-security-match",
    )


def test_a_policy_string_matched_by_substring_is_flagged() -> None:
    """A policy expressed as a string and read with `in` has no vocabulary: it
    cannot tell `is_admin` from `is_admin_readonly`, and adding the second one
    silently widens the first."""
    assert_flags(
        """
        def authorise(condition: str = ""):
            if "principal.is_admin" in condition:
                return check_admin
            return check_member
        """,
        "substring-security-match",
    )


def test_a_denylist_that_lost_its_comma_is_flagged() -> None:
    """`("authorization")` is a string, not a one-element tuple, so this is a
    substring test. It both over-matches (any header whose name is a substring
    of that word is dropped) and reads as correct, because it does happen to
    drop the header it was written for."""
    assert_flags(
        """
        def loggable(request):
            return {
                header: value
                for header, value in request.headers.items()
                if header.lower() not in ("authorization")
            }
        """,
        "substring-security-match",
    )


def test_a_prefix_check_is_clean() -> None:
    assert_clean(
        """
        def is_exempt(path: str) -> bool:
            return any(path.startswith(route) for route in EXEMPT_ROUTES)
        """,
        "substring-security-match",
    )


def test_membership_in_a_collection_is_clean() -> None:
    """`"admin" in roles` where `roles` is a collection is correct code, and
    nothing in the file claims it is a string. Deciding on provenance rather
    than on shape is the same move `sql-interpolation` makes."""
    for source in (
        'def check(principal):\n    return "admin" in principal.roles\n',
        "def check(scopes):\n    return SCOPE_ADMIN in scopes\n",
        'def check(method, allowed):\n    return method in allowed\n',
    ):
        assert_clean(source, "substring-security-match")


def test_a_character_class_is_clean() -> None:
    assert_clean(
        """
        def is_hex(value):
            return all(character in "0123456789abcdef" for character in value)
        """,
        "substring-security-match",
    )


# --- auth-fallback-on-exception ----------------------------------------------


def test_an_authentication_path_that_falls_back_to_a_second_verifier_is_flagged() -> None:
    """Two defects in one shape. The path degrades to a *weaker* verifier when
    the strong one raises, so anything that can make the strong path fail
    selects the weak one -- and the explicit `raise Unauthorized` inside the
    `try` is itself caught, so a denial becomes a retry rather than a 401."""
    assert_flags(
        """
        async def authenticate(request, token):
            try:
                claims = await verify_with_jwks(token)
                ranger = await lookup(claims["sub"])
                if ranger is None:
                    raise Unauthorized("no such ranger")
                return ranger
            except Exception:
                claims = jwt.decode(token, SHARED_SECRET, algorithms=["HS256"])
                return await lookup(claims["sub"])
        """,
        "auth-fallback-on-exception",
    )


def test_a_denial_swallowed_by_its_own_broad_handler_is_flagged() -> None:
    """The second half on its own: no fallback verifier, but the refusal raised
    in the `try` never reaches the caller."""
    assert_flags(
        """
        async def authenticate(request, token):
            try:
                if not token:
                    raise Unauthorized("no token")
                return await lookup(token)
            except Exception:
                return ANONYMOUS
        """,
        "auth-fallback-on-exception",
    )


def test_a_narrow_handler_that_denies_is_clean() -> None:
    assert_clean(
        """
        async def authenticate(request, token):
            try:
                claims = await verify_with_jwks(token)
            except JWTError:
                raise Unauthorized("invalid or expired token")
            return await lookup(claims["sub"])
        """,
        "auth-fallback-on-exception",
    )


def test_a_broad_handler_that_re_raises_is_clean() -> None:
    """Catching broadly to add context and re-raising keeps one exit."""
    assert_clean(
        """
        async def authenticate(request, token):
            try:
                return await verify_with_jwks(token)
            except Exception:
                logger.exception("jwks verification failed")
                raise
        """,
        "auth-fallback-on-exception",
    )


def test_a_broad_handler_outside_an_authentication_path_is_clean() -> None:
    """`AGENTS.md` legislates broad handlers generally; this rule is only about
    the ones that decide who the caller is."""
    assert_clean(
        """
        async def render_sightings(session):
            try:
                return await load(session)
            except Exception:
                return []
        """,
        "auth-fallback-on-exception",
    )


# --- mass-assignment, from a handler parameter -------------------------------


def test_a_declared_body_walked_onto_a_model_is_flagged() -> None:
    """The rule already catches `await request.json()`. A bound body model is
    the same value with a schema in front of it, and walking *it* onto a row
    throws the schema away again."""
    assert_flags(
        """
        @router.patch("/stations/{station_id}")
        async def update(request, session, station_id: str, patch: StationPatch):
            station = await session.fetch_one(Station.select().where(Station.id == station_id))
            for key, value in patch.items():
                setattr(station, key, value)
            return station
        """,
        "mass-assignment",
    )


# =============================================================================
# Precision, pinned
# =============================================================================
#
# Every test below exists because `wreath mutant --changed` removed a guard and
# no test objected. A rule's *quiet* half is the half that decides whether it
# survives contact with a real codebase, and an `assert_clean` passes trivially
# against a rule that never fires at all -- so a clean test nobody has falsified
# is not evidence. Each of these names the specific guard it holds down.


def test_a_readable_identifier_under_a_secret_name_is_clean() -> None:
    """The four literals that narrowed this rule, in their reduced shape.

    Each is long, each sits under a name from the credential vocabulary, and
    none is a secret: a sentinel standing for "no password", a `request.state`
    key, a URN. Length alone cannot tell them from a key -- the alphabet and the
    digits can.
    """
    for source in (
        '_STATE_TOKEN = "_camera_trap_csrf_token"\n',
        'UNUSABLE_PASSWORD = "!never-provisioned"\n',
        '_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"\n',
        'SESSION_SECRET_HEADER = "x-camera-trap-session"\n',
    ):
        assert_clean(source, "hardcoded-secret")


def test_a_key_needs_both_letters_and_digits() -> None:
    """The digit half of the same guard, on its own."""
    assert_clean('SIGNING_KEY = "correcthorsebatterystaple"\n', "hardcoded-secret")
    assert_flags('SIGNING_KEY = "correcthorse4batterystaple"\n', "hardcoded-secret")


def test_a_secret_in_a_call_that_is_not_a_logger_is_clean() -> None:
    """`secret-in-log` is about the sink. Formatting a token into an outbound
    header is what a client is for, and it is a call like any other."""
    assert_clean(
        """
        def call(client, session_token):
            return client.post(UPSTREAM, headers={"authorization": f"Bearer {session_token}"})
        """,
        "secret-in-log",
    )


def test_a_secret_logged_as_the_whole_message_is_flagged() -> None:
    """No format string at all. The value still reaches the record, and an
    earlier draft only looked at arguments *after* the first."""
    assert_flags(
        """
        def verify(api_key):
            logger.info(api_key)
        """,
        "secret-in-log",
    )


def test_a_catalog_is_not_a_logger() -> None:
    """`log` is a substring of `catalog`, `dialog` and `backlog`. Matching the
    receiver loosely would have made every one of them a logging call."""
    assert_clean(
        """
        def record(catalog, api_key):
            catalog.info(f"registered {api_key}")
        """,
        "secret-in-log",
    )


def test_the_strong_secret_vocabulary_is_reachable_on_its_own() -> None:
    """`hmac` and `passwd` are in this module's own vocabulary and not in
    `crud.SENSITIVE_FIELD`; the rule reads both."""
    assert_flags(
        """
        def sign(hmac_value):
            logger.debug(f"computed {hmac_value}")
        """,
        "secret-in-log",
    )


def test_a_mapping_lookup_with_a_tainted_key_is_clean() -> None:
    """The receiver decides, not the verb. `body.get(<anything>)` is a mapping
    lookup however caller-controlled its argument is."""
    assert_clean(
        """
        @router.post("/sightings")
        async def ingest(request, field: str):
            body = await request.json()
            return body.get(field)
        """,
        "outbound-url-from-request",
    )


def test_an_outbound_call_with_no_arguments_is_clean() -> None:
    assert_clean(
        """
        @router.get("/health")
        async def health(request, client, probe: str):
            return await client.get()
        """,
        "outbound-url-from-request",
    )


def test_a_wildcard_origin_with_credentials_disabled_is_clean() -> None:
    """The credentials half of the pair, asserted from the other side."""
    assert_clean(
        """
        app.add_global_middleware(
            CORSMiddleware(allow_origins=["*"], allow_credentials=False)
        )
        """,
        "wildcard-trust-list",
    )


def test_only_proxy_headers_middleware_establishes_the_boundary() -> None:
    """A `trusted=` keyword on some other constructor is not a proxy trust
    boundary, and must not silence the forwarded-header rule."""
    assert_flags(
        """
        app.add_global_middleware(TrustedHostMiddleware(trusted=["trek.example"]))

        @router.get("/herds")
        async def herds(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_two_secret_names_sharing_a_provenance_are_clean() -> None:
    """The provenance clause itself. Both operands are weak-secret names, so the
    pair reaches the comparison -- and both say `header`, which means one value
    is being taken apart rather than checked against a presented one."""
    assert_clean(
        """
        def check(header_token, header_signature):
            return header_token != header_signature
        """,
        "timing-unsafe-compare",
    )


def test_two_secret_names_with_no_provenance_are_clean() -> None:
    """Neither operand says where it came from, so the pair carries no signal
    and the rule falls back to needing a comparison role."""
    assert_clean(
        """
        def check(token, signature):
            return token != signature
        """,
        "timing-unsafe-compare",
    )


def test_a_refusing_function_returning_on_an_ordinary_condition_is_clean() -> None:
    """`authz-fail-open` is about the branch where the subject could not be
    *resolved*, not about every early return in a function that refuses."""
    assert_clean(
        """
        async def authorise_reserve(request, reserve_id, page):
            if page > MAX_PAGE:
                return []
            if reserve_id not in request.state.principal.reserves:
                raise Forbidden("not a member of this reserve")
            return request.state.principal.id
        """,
        "authz-fail-open",
    )


def test_a_refusal_by_status_code_counts_as_refusing() -> None:
    """Not every codebase names its refusal. `HTTPException(status_code=403)`
    is one, and a check that raises it is an authorization check."""
    assert_flags(
        """
        async def authorise_reserve(request, reserve_id):
            resolved = resolve(reserve_id)
            if resolved is None:
                return
            if resolved not in request.state.principal.reserves:
                raise HTTPException(status_code=403, detail="not a member")
            return request.state.principal.id
        """,
        "authz-fail-open",
    )


def test_a_narrow_handler_that_falls_back_is_clean() -> None:
    """The breadth guard on its own. Catching the one error a verifier raises
    and answering it with a second, *declared* path is a decision; catching
    everything and retrying is not."""
    assert_clean(
        """
        async def authenticate(request, token):
            try:
                return await verify_with_jwks(token)
            except JWKSUnavailable:
                return await verify_with_cached_keys(token)
        """,
        "auth-fallback-on-exception",
    )


def test_a_broad_handler_that_does_not_verify_is_clean() -> None:
    """The verifier guard on its own: an authentication function may still have
    an ordinary broad handler that is not a second verification attempt."""
    assert_clean(
        """
        async def authenticate_and_record(request, token):
            principal = await lookup(token)
            try:
                await record_login(principal)
            except Exception:
                metrics.increment("login_record_failed")
            return principal
        """,
        "auth-fallback-on-exception",
    )


def test_a_setattr_loop_over_configuration_is_clean() -> None:
    """The taint guard on `mass-assignment`. Walking a dict onto an object is
    how configuration is loaded; it is the *provenance* of the dict that makes
    it a defect."""
    assert_clean(
        """
        DEFAULTS = {"retries": 3, "timeout": 5}

        def configure(station):
            for key, value in DEFAULTS.items():
                setattr(station, key, value)
        """,
        "mass-assignment",
    )


def test_membership_in_a_non_string_annotation_is_clean() -> None:
    """The annotation is what decides. `list[str]` is a collection, and
    membership in a collection is the correct spelling."""
    assert_clean(
        """
        def is_exempt(scopes: list[str]) -> bool:
            return "admin" in scopes
        """,
        "substring-security-match",
    )


def test_a_syntax_fragment_searched_in_a_path_is_clean() -> None:
    """Every path-matching implementation reads its own template for a syntax
    character. Five places in Wreath's own source are this shape, and they are
    lexical checks on a pattern rather than decisions about a request."""
    for source in (
        'def register(path: str):\n    return "{" not in path\n',
        'def register(path: str):\n    return ":path}" in path\n',
        'def register(path: str):\n    return "\\\\" in path\n',
    ):
        assert_clean(source, "substring-security-match")


def test_a_debug_gated_disclosure_is_clean() -> None:
    """Disclosure behind a debug flag is a decision somebody made and can see.
    Shipping the flag on is the finding, and `debug-enabled` reports that."""
    assert_clean(
        """
        def handle(request, error_handler):
            try:
                return error_handler(request)
            except Exception as failure:
                if request.app.debug:
                    return ProblemResponse(status=500, detail=f"handler raised: {failure}")
                return ProblemResponse(status=500, detail="internal error")
        """,
        "error-detail-leaked",
    )


def test_an_ungated_disclosure_beside_a_debug_branch_still_fires() -> None:
    """The other half: the gate has to be over *this* statement."""
    assert_flags(
        """
        def handle(request, error_handler):
            try:
                return error_handler(request)
            except Exception as failure:
                if request.app.debug:
                    log.exception("handler raised")
                return ProblemResponse(status=500, detail=f"handler raised: {failure}")
        """,
        "error-detail-leaked",
    )


def test_a_fallback_verifier_is_flagged_without_a_swallowed_refusal() -> None:
    """`auth-fallback-on-exception` has two halves and this isolates the first.

    The headline test for this rule carries both -- a fallback verifier *and* a
    refusal raised inside the `try` -- so it passed even when the verifier
    detection was removed entirely. The two need separate tests or one of them
    is never actually exercised.
    """
    assert_flags(
        """
        async def authenticate(request, token):
            try:
                return await verify_with_jwks(token)
            except Exception:
                return jwt.decode(token, SHARED_SECRET, algorithms=["HS256"])
        """,
        "auth-fallback-on-exception",
    )


def test_a_broad_handler_that_re_raises_after_verifying_is_clean() -> None:
    """The re-raise guard, isolated the same way: a handler that tries a second
    verifier and then re-raises has not fallen back to it."""
    assert_clean(
        """
        async def authenticate(request, token):
            try:
                return await verify_with_jwks(token)
            except Exception:
                metrics.increment("jwks_failed")
                if not await verify_with_cached_keys(token):
                    raise
                raise
        """,
        "auth-fallback-on-exception",
    )


def test_a_verifier_in_a_non_authentication_function_is_clean() -> None:
    """The `authentication` half of the same guard. `decode` is what a codec
    does, and a broad handler around one is an ordinary defect rather than an
    authentication fallback."""
    assert_clean(
        """
        async def render_sighting(payload):
            try:
                return parse_strict(payload)
            except Exception:
                return json.decode(payload, strict=False)
        """,
        "auth-fallback-on-exception",
    )


def test_a_function_that_raises_something_else_is_not_an_authorizer() -> None:
    """`authz-fail-open`'s precondition, isolated: raising *any* exception must
    not make a function an authorization check, or the rule lands on every
    validator that returns early."""
    assert_clean(
        """
        def parse_reserve(raw):
            if raw is None:
                return None
            if not raw.isdigit():
                raise ValueError("reserve ids are numeric")
            return int(raw)
        """,
        "authz-fail-open",
    )


def test_a_server_error_status_is_not_a_refusal() -> None:
    """Only 401 and 403 make a raise an authorization refusal."""
    assert_clean(
        """
        async def load_reserve(request, reserve_id):
            resolved = resolve(reserve_id)
            if resolved is None:
                return
            if resolved.broken:
                raise HTTPException(status_code=500, detail="reserve is corrupt")
            return resolved
        """,
        "authz-fail-open",
    )


def test_a_strong_secret_name_needs_no_second_signal() -> None:
    """The `strong` branch of `timing-unsafe-compare`, which the provenance
    work sits beside: a name from the strong vocabulary fires on its own."""
    assert_flags(
        """
        def check(supplied, stored):
            return supplied.password == stored.password
        """,
        "timing-unsafe-compare",
    )


def test_one_operand_with_a_provenance_is_not_a_pair() -> None:
    """`all(provenances)` -- both sides have to say where they came from, or
    there is no pair and no signal."""
    assert_clean(
        """
        def check(header_token, signature):
            return header_token != signature
        """,
        "timing-unsafe-compare",
    )


def test_a_key_like_literal_under_an_ordinary_name_is_clean() -> None:
    """The name half of the declaration rule: high entropy is not by itself a
    secret. A checksum, a fixture id and a test vector all look like this."""
    assert_clean('SIGHTING_FIXTURE = "a1b2c3d4e5f6a7b8c9"\n', "hardcoded-secret")


def test_a_declared_secret_in_bytes_is_flagged() -> None:
    assert_flags('SIGNING_KEY = b"8c1f0a77e5b3d942ac6e10bf35d8724e"\n', "hardcoded-secret")


def test_an_all_digit_literal_is_not_a_key() -> None:
    """A version stamp, a numeric id, a timestamp."""
    assert_clean('TOKEN_EPOCH = "20260804120000000000"\n', "hardcoded-secret")


def test_a_security_flag_decided_by_the_request_is_clean() -> None:
    """`env-conditional-security` is about the *deployment* deciding. A flag
    computed from the request in front of you is ordinary logic."""
    assert_clean(
        """
        def cookie_for(request):
            secure = True if request.url.scheme == "https" else False
            return secure
        """,
        "env-conditional-security",
    )


def test_a_ternary_security_flag_is_flagged_on_its_own() -> None:
    """Split from the paired test, which carried a ternary *and* a comparison
    and so stayed green when the ternary branch was removed."""
    assert_flags(
        """
        ENV = os.getenv("ENV", "develop")
        secure: bool = True if ENV == "production" else False
        """,
        "env-conditional-security",
    )


def test_a_comparison_security_flag_is_flagged_on_its_own() -> None:
    assert_flags(
        """
        ENV = os.getenv("ENV", "develop")
        httponly: bool = ENV not in ("develop", "test")
        """,
        "env-conditional-security",
    )


def test_a_wildcard_in_an_unrelated_keyword_is_clean() -> None:
    """Methods and headers are not a trust list. Only origins pair with
    credentials, and only trust boundaries stand alone."""
    assert_clean(
        """
        app.add_global_middleware(
            CORSMiddleware(
                allow_origins=["https://console.trek.example"],
                allow_methods=["*"],
                allow_headers=["*"],
                allow_credentials=True,
            )
        )
        """,
        "wildcard-trust-list",
    )


def test_a_policy_declared_as_a_module_string_is_matched() -> None:
    """The annotated-assignment half of the string provenance, which the
    parameter tests do not reach."""
    assert_flags(
        """
        policy: str = load_policy()

        def may_write(action):
            return action in policy
        """,
        "substring-security-match",
    )


def test_a_secret_named_template_is_not_a_secret_value() -> None:
    """Only the interpolated *values* count. A format string whose own name
    reads as a credential is still just the message."""
    assert_clean(
        """
        SECRET_ROTATION_TEMPLATE = "rotated the key for station {}"

        def announce(station_id):
            logger.info(SECRET_ROTATION_TEMPLATE.format(station_id))
        """,
        "secret-in-log",
    )


def test_a_secret_in_the_extra_mapping_is_flagged() -> None:
    """`extra=` is how structured logging carries fields, and it reaches the
    record exactly as the message does."""
    assert_flags(
        """
        def verify(session_token):
            logger.info("verified", extra={"token": session_token})
        """,
        "secret-in-log",
    )


def test_an_inline_client_with_a_tainted_url_is_flagged() -> None:
    """`httpx.AsyncClient().get(...)` has no bound receiver name, so the
    fallback that reads the immediate attribute is what has to catch it."""
    assert_flags(
        """
        @router.post("/stations/register")
        async def register(request, callback: str):
            return await httpx.AsyncClient().get(callback)
        """,
        "outbound-url-from-request",
    )


def test_only_a_trusted_keyword_establishes_the_boundary() -> None:
    """`ProxyHeadersMiddleware(trust_host=False, trusted=["*"])` -- the trust
    list is the keyword that decides, not whichever one comes first."""
    assert_flags(
        """
        app.add_global_middleware(
            ProxyHeadersMiddleware(trust_host=False, trusted=["*"])
        )

        @router.get("/herds")
        async def herds(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_a_refusal_raised_as_a_bare_class_still_counts() -> None:
    """`raise Forbidden` is legal Python and is a refusal. Reading the raised
    expression as though it were always a call is how a rule crashes on it."""
    assert_flags(
        """
        async def authorise_reserve(request, reserve_id):
            resolved = resolve(reserve_id)
            if resolved is None:
                return
            if resolved not in request.state.principal.reserves:
                raise Forbidden
            return request.state.principal.id
        """,
        "authz-fail-open",
    )


def test_a_debug_gate_must_cover_the_disclosure_itself() -> None:
    """The gate is over a *statement*, not over the handler. A debug branch
    elsewhere in the same handler does not excuse the response beside it."""
    assert_flags(
        """
        def handle(request, error_handler):
            try:
                return error_handler(request)
            except Exception as failure:
                if request.app.debug:
                    log.exception("handler raised")
                if failure.retryable:
                    return ProblemResponse(status=503, detail=f"retry later: {failure}")
                return ProblemResponse(status=500, detail="internal error")
        """,
        "error-detail-leaked",
    )


def test_a_non_string_annotated_module_value_is_clean() -> None:
    """The annotation decides for module values exactly as it does for
    parameters: a `Policy` object is not a string, and `in` over one is
    whatever that object means by it."""
    assert_clean(
        """
        policy: Policy = load_policy()

        def may_write(action):
            return action in policy
        """,
        "substring-security-match",
    )


def test_a_tainted_value_on_a_non_request_method_is_clean() -> None:
    """Only the methods that issue a request are a destination. Configuring a
    client with a caller's value is not a fetch.

    The value has to be the *first* argument for this to test anything: an
    earlier version passed a header name first, so the rule stayed quiet
    because it read the constant, not because the method was not a verb.
    """
    assert_clean(
        """
        @router.post("/stations/register")
        async def register(request, client, callback: str):
            client.set_base_url(callback)
        """,
        "outbound-url-from-request",
    )


def test_a_tainted_query_parameter_on_a_safe_url_is_clean() -> None:
    """The rule is about the *destination*. A caller-supplied query value on a
    URL the application chose is an ordinary proxied search."""
    assert_clean(
        """
        UPSTREAM = "https://sightings.reserve.example/search"

        @router.get("/search")
        async def search(request, client, q: str):
            return await client.get(UPSTREAM, params={"q": q})
        """,
        "outbound-url-from-request",
    )


def test_a_wildcard_origin_beside_an_unrelated_true_flag_is_clean() -> None:
    """Credentials are the keyword that pairs with a wildcard origin. Any other
    flag being on is not the same decision."""
    assert_clean(
        """
        app.add_global_middleware(
            CORSMiddleware(allow_origins=["*"], allow_private_network=True)
        )
        """,
        "wildcard-trust-list",
    )


def test_both_operands_must_be_secret_named_to_pair() -> None:
    """Differing provenance is only a signal when both sides are secrets. A
    cookie value compared against a header token is two different things being
    compared, and one of them is not a credential."""
    assert_clean(
        """
        def check(header_token, cookie_value):
            return header_token != cookie_value
        """,
        "timing-unsafe-compare",
    )


def test_a_bare_non_refusal_class_is_read_without_crashing() -> None:
    """`raise CustomError` binds a class, not a call. Reading its arguments for
    a status code is how a rule dies on perfectly ordinary source -- and a rule
    that raises takes the scan of the whole tree with it."""
    assert_clean(
        """
        def parse_reserve(raw):
            if raw is None:
                return None
            raise ReserveUnreadable
        """,
        "authz-fail-open",
    )


def test_credentials_decided_at_runtime_are_read_without_crashing() -> None:
    """`allow_credentials=credentialed()` is not a literal, and reading `.value`
    off whatever it is instead is the same class of crash.

    A *call* rather than an attribute, deliberately: an `ast.Attribute` happens
    to carry a `.value` of its own, so it reads without complaint and proves
    nothing about the guard.
    """
    assert_clean(
        """
        app.add_global_middleware(
            CORSMiddleware(allow_origins=["*"], allow_credentials=credentialed())
        )
        """,
        "wildcard-trust-list",
    )


def test_any_part_of_a_client_chain_names_the_receiver() -> None:
    """`self.http.transport.get(url)` is an outbound request. Reading only the
    last name before the verb would take `transport` and miss it."""
    assert_flags(
        """
        @router.post("/stations/register")
        async def register(request, callback: str):
            return await http.transport.get(callback)
        """,
        "outbound-url-from-request",
    )


# --- path-from-request -------------------------------------------------------


def test_a_path_joined_with_a_caller_supplied_name_is_flagged() -> None:
    assert_flags(
        """
        EXPORT_ROOT = Path("/srv/exports")

        @router.get("/download")
        async def download(request, name: str):
            return (EXPORT_ROOT / name).read_bytes()
        """,
        "path-from-request",
    )


def test_os_path_join_with_a_caller_supplied_name_is_flagged() -> None:
    """The same defect, and worse in one specific way: an absolute component
    does not extend the root, it replaces it."""
    assert_flags(
        """
        EXPORT_ROOT = "/srv/exports"

        @router.get("/download")
        async def download(request, name: str):
            return Path(os.path.join(EXPORT_ROOT, name)).read_bytes()
        """,
        "path-from-request",
    )


def test_a_root_named_without_a_root_word_is_still_a_path() -> None:
    """`catalogue` says nothing about being a directory, so the only thing that
    makes this a join rather than division is the `Path(...)` it came from."""
    assert_flags(
        """
        catalogue = Path("/srv/exports")

        @router.get("/download")
        async def download(request, name: str):
            return (catalogue / name).read_bytes()
        """,
        "path-from-request",
    )


def test_a_path_joined_with_a_constant_is_clean() -> None:
    assert_clean(
        """
        EXPORT_ROOT = Path("/srv/exports")

        @router.get("/manifest")
        async def manifest(request):
            return (EXPORT_ROOT / "manifest.json").read_bytes()
        """,
        "path-from-request",
    )


def test_a_normalised_key_read_through_storage_is_clean() -> None:
    assert_clean(
        """
        exports = LocalStorage(Path("/srv/exports"))

        @router.get("/download")
        async def download(request, name: str):
            return await exports.get(normalize_key(name))
        """,
        "path-from-request",
    )


def test_os_path_join_with_constant_components_is_clean() -> None:
    """Holds the taint condition in `_path_join_call`. Without it every
    `os.path.join` in the tree is a traversal."""
    assert_clean(
        """
        EXPORT_ROOT = "/srv/exports"

        @router.get("/manifest")
        async def manifest(request):
            return Path(os.path.join(EXPORT_ROOT, "manifest.json")).read_bytes()
        """,
        "path-from-request",
    )


def test_ordinary_division_by_a_caller_supplied_number_is_clean() -> None:
    """Holds the `_is_pathlike` half of the guard. `/` is division far more
    often than it is a path join, and the left operand is what says which."""
    assert_clean(
        """
        @router.get("/rate")
        async def rate(request, count: int):
            return {"per": total / count}
        """,
        "path-from-request",
    )


def test_an_operator_that_is_not_a_join_is_clean() -> None:
    """Holds the `ast.Div` half. `base_path_total` is path-shaped by name, and
    subtraction is still arithmetic."""
    assert_clean(
        """
        @router.get("/offset")
        async def offset(request, index: int):
            return {"at": base_path_total - index}
        """,
        "path-from-request",
    )


def test_a_name_assigned_from_something_other_than_path_is_not_a_path() -> None:
    """Holds the `Path`/`PurePath` clause on the `path_names` collection.
    Without it every name assigned from any call is a path."""
    assert_clean(
        """
        catalogue = build_index("/srv/exports")

        @router.get("/rate")
        async def rate(request, count: int):
            return {"per": catalogue / count}
        """,
        "path-from-request",
    )


def test_a_tuple_assignment_from_path_does_not_break_the_scan() -> None:
    """Holds the `isinstance(target, ast.Name)` filter on the same collection.
    Without it a tuple target raises while the module is being read, and the
    scan reports nothing at all for the file -- which reads exactly like a clean
    file."""
    assert_flags(
        """
        catalogue = Path("/srv/exports")
        head, tail = Path("/srv/exports")

        @router.get("/download")
        async def download(request, name: str):
            return (catalogue / name).read_bytes()
        """,
        "path-from-request",
    )


# --- timing-unsafe-compare, the hand-rolled loop -----------------------------


def test_an_elementwise_comparison_returning_early_is_flagged() -> None:
    """The shape `_timing` cannot see: the operands are `a` and `b`, so no name
    in the comparison says secret, and the loop is the whole defect."""
    assert_flags(
        """
        def verify(given: str, expected: str) -> bool:
            for a, b in zip(given, expected, strict=True):
                if a != b:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_compare_digest_instead_of_a_loop_is_clean() -> None:
    assert_clean(
        """
        def verify(given: str, expected: str) -> bool:
            return hmac.compare_digest(given, expected)
        """,
        "timing-unsafe-compare",
    )


def test_a_loop_that_is_not_over_a_pair_is_clean() -> None:
    """Holds the `zip` clause, and holds it with two bound names so that the
    membership test below cannot be what makes this quiet. `enumerate` pairs an
    index with a value; stopping early on that leaks a position, not a secret."""
    assert_clean(
        """
        def first_mismatch(rows) -> bool:
            for index, row in enumerate(rows):
                if index != row:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_a_loop_whose_iterable_is_not_a_call_is_clean() -> None:
    """Holds the `isinstance(node.iter, ast.Call)` clause -- without it the
    guard reads `.func` off a plain name and raises."""
    assert_clean(
        """
        def check(pairs) -> bool:
            for a, b in pairs:
                if a != b:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_a_zip_loop_with_one_bound_name_is_clean() -> None:
    """One name cannot be an elementwise comparison of two sequences, and the
    membership test is what says so -- `sentinel` is not one of the loop's
    elements."""
    assert_clean(
        """
        def check(rows) -> bool:
            for row in zip(rows):
                if row != sentinel:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_a_zip_loop_whose_body_is_not_a_test_is_clean() -> None:
    """Holds the `isinstance(statement, ast.If)` clause."""
    assert_clean(
        """
        def merge(left, right) -> list:
            out = []
            for a, b in zip(left, right, strict=True):
                out.append(a + b)
            return out
        """,
        "timing-unsafe-compare",
    )


def test_a_zip_loop_testing_a_flag_is_clean() -> None:
    """Holds the `isinstance(statement.test, ast.Compare)` clause: the branch is
    a truth test, not a comparison of the two elements."""
    assert_clean(
        """
        def check(left, right) -> bool:
            for a, b in zip(left, right, strict=True):
                if aborted:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_a_zip_loop_comparing_something_else_is_clean() -> None:
    """Holds `operands <= elements`. The loop is over the pair; the branch is
    about a budget, and stopping early on it leaks nothing about either
    sequence."""
    assert_clean(
        """
        def check(left, right) -> bool:
            for a, b in zip(left, right, strict=True):
                if seen != limit:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_a_zip_loop_ordering_its_elements_is_clean() -> None:
    """Holds the `Eq`/`NotEq` clause. `<` is a sort, not an equality check."""
    assert_clean(
        """
        def ordered(left, right) -> bool:
            for a, b in zip(left, right, strict=True):
                if a < b:
                    return False
            return True
        """,
        "timing-unsafe-compare",
    )


def test_a_zip_loop_that_counts_differences_is_clean() -> None:
    """Holds the `ast.Return` clause, which is the whole defect: a loop that
    visits every element takes the same time whatever the inputs are."""
    assert_clean(
        """
        def differences(left, right) -> int:
            count = 0
            for a, b in zip(left, right, strict=True):
                if a != b:
                    count += 1
            return count
        """,
        "timing-unsafe-compare",
    )


# --- weak-randomness, the encoded draw ---------------------------------------


def test_a_random_draw_with_an_encoding_on_the_end_is_flagged() -> None:
    """`.hex()` does not make a Mersenne Twister unpredictable, and the draw is
    one call further in than the rule's first look."""
    assert_flags(
        """
        def mint() -> str:
            api_secret = random.randbytes(16).hex()
            return api_secret
        """,
        "weak-randomness",
    )


def test_an_encoded_value_from_elsewhere_is_clean() -> None:
    """Holds the clause that keeps the walk to `random`. Without it any chained
    call assigned to a credential name is a weak draw."""
    assert_clean(
        """
        def mint() -> str:
            api_secret = derive(material).hex()
            return api_secret
        """,
        "weak-randomness",
    )


# --- hardcoded-secret, the development key -----------------------------------


def test_a_development_key_with_no_digits_is_flagged() -> None:
    """The shape a key actually ships in. It carries no digits, so the
    key-alphabet test reads it as an identifier; it says what it is in words,
    which is what makes it decidable."""
    assert_flags(
        """
        SESSION_SECRET = "northwind-dev-secret"
        """,
        "hardcoded-secret",
    )


def test_the_name_of_the_variable_a_secret_is_read_from_is_clean() -> None:
    """`SESSION_SECRET_VARIABLE = "APP_SESSION_SECRET"` is the mechanism for
    keeping a key out of the source, not a key in it."""
    assert_clean(
        """
        SESSION_SECRET_VARIABLE = "APP_SESSION_SECRET"
        """,
        "hardcoded-secret",
    )
