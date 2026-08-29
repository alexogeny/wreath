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
    assert_clean(
        """
        async def seed(connection, table, columns):
            await connection.execute(f"INSERT INTO {table} ({columns}) VALUES ($1)", 1)
        """,
        "sql-interpolation",
    )


def test_f_string_with_no_interpolation_is_clean() -> None:
    assert_clean(
        """
        async def search(session):
            return await session.raw(f"SELECT 1").fetch()
        """,
        "sql-interpolation",
    )


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


def test_literal_session_secret_is_flagged() -> None:
    assert_flags(
        """
        from wreath.policy import HttpPolicy, SessionPolicy
        app.configure_http_policy(HttpPolicy(session=SessionPolicy("northwind-dev-secret")))
        """,
        "hardcoded-secret",
    )


def test_secret_read_from_the_environment_is_clean() -> None:
    assert_clean(
        """
        import os
        from wreath.policy import HttpPolicy, SessionPolicy
        app.configure_http_policy(HttpPolicy(session=SessionPolicy(os.environ["SESSION_SECRET"])))
        """,
        "hardcoded-secret",
    )


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
        from wreath.policy import CorsPolicy
        app.add_global_middleware(
            CorsPolicy(allow_origins=("https://console.example",), allow_credentials=True)
        )
        """,
        "cors-reflect-origin",
    )


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


def test_rate_limit_key_from_a_forwarded_header_is_flagged() -> None:
    assert_flags(
        """
        from wreath.policy import HttpPolicy, RateLimitPolicy
        def key(request):
            return request.header("x-forwarded-for")
        app.configure_http_policy(HttpPolicy(
            rate_limit=RateLimitPolicy(limit=5, key=key)
        ))
        """,
        "untrusted-forwarded-header",
    )


def test_forwarded_header_with_proxy_middleware_configured_is_clean() -> None:
    assert_clean(
        """
        from wreath.policy import HttpPolicy, ProxyPolicy, RateLimitPolicy
        app.configure_http_policy(HttpPolicy(
            proxy=ProxyPolicy(trusted=("10.0.0.1",))
        ))
        def key(request):
            return request.header("x-forwarded-for")
        app.configure_http_policy(HttpPolicy(
            rate_limit=RateLimitPolicy(limit=5, key=key)
        ))
        """,
        "untrusted-forwarded-header",
    )


def test_every_rule_has_a_reference_and_a_suggestion() -> None:
    corpus = """
        import importlib, random, tarfile, xml.sax, zipfile
        from wreath import Wreath
        from wreath.http_client import DestinationPolicy
        from wreath.policy import HttpPolicy, SessionPolicy
        from wreath.templates import Template

        app = Wreath(debug=True)
        app.configure_http_policy(HttpPolicy(session=SessionPolicy("dev-secret")))
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
    findings = scan_source(
        """
        import os
        from typing import Annotated

        from wreath import Request, Router
        from wreath.auth import authenticated
        from wreath.policy import HttpPolicy, SessionPolicy
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
    findings = scan_source("def broken(:\n", surface="broken.py")
    assert [f.rule_id for f in findings] == ["unparseable"]


@pytest.mark.parametrize("rule", CODE_RULES, ids=lambda rule: rule.rule_id)
def test_rule_documents_its_own_reference(rule) -> None:
    assert rule.reference, f"{rule.rule_id} declares no CWE or wreath reference"


# Every test below is a false positive the first draft produced against
# `src/wreath`, reduced to its shape. Twenty-nine findings, of which
# twenty-eight were the detector's fault; keeping them as tests is what stops
# the next widening of a vocabulary from bringing them all back.


def test_attribute_assignment_does_not_taint_the_object() -> None:
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
    for source in (
        "def f(other, signature):\n    return other == signature\n",
        "def f(where, signature):\n    return _normalise(where) == signature\n",
        "def f(digest, plan):\n    return digest != plan.digest\n",
        "def f(token, kind):\n    return token[0] != kind\n",
    ):
        assert_clean(source, "timing-unsafe-compare")


def test_a_secret_compared_against_a_named_expectation_still_fires() -> None:
    assert_flags(
        "def f(given, expected_signature):\n    return given == expected_signature\n",
        "timing-unsafe-compare",
    )


def test_identifier_suffixes_are_not_secrets() -> None:
    for source in (
        "def f(rows, credential_id):\n    return [r for r in rows if r.id != credential_id]\n",
        "def f(parts):\n    return parts[0] != _TOKEN_VERSION\n",
        "def f(model, credential_id):\n"
        "    return model.select().where(model.id == credential_id)\n",
    ):
        assert_clean(source, "timing-unsafe-compare")


def test_template_compiled_from_a_module_constant_is_clean() -> None:
    assert_clean(
        """
        _DOCS_SHELL = "<html>{{ title }}</html>"
        def build(title):
            return Template.from_string(_DOCS_SHELL).render(title=title)
        """,
        "template-from-request",
    )


def test_method_allowlist_is_not_an_identity_comparison() -> None:
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
    assert_flags(
        """
        @router.post("/reset")
        async def reset(request, account_id: int):
            generator = random.Random(account_id)
            return "".join(generator.choice("0123456789abcdef") for _ in range(32))
        """,
        "weak-randomness",
    )


def test_wildcard_host_allowlist_is_flagged() -> None:
    assert_flags(
        """
        from wreath.policy import HttpPolicy, TrustedHostPolicy
        app.configure_http_policy(HttpPolicy(
            trusted_host=TrustedHostPolicy(allowed_hosts=["*"])
        ))
        """,
        "wildcard-trust-list",
    )


def test_wildcard_cors_origins_are_flagged() -> None:
    assert_flags(
        """
        from wreath.policy import CorsPolicy, HttpPolicy
        app.configure_http_policy(HttpPolicy(
            cors=CorsPolicy(allow_origins=["*"], allow_credentials=True)
        ))
        """,
        "wildcard-trust-list",
    )


def test_real_trust_lists_are_clean() -> None:
    assert_clean(
        """
        from wreath.policy import CorsPolicy, HttpPolicy, ProxyPolicy
        app.configure_http_policy(HttpPolicy(
            proxy=ProxyPolicy(trusted=["10.0.0.0/8"]),
            cors=CorsPolicy(allow_origins=["https://console.example"]),
        ))
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_proxy_boundary_does_not_silence_the_forwarded_rule() -> None:
    source = """
        from wreath.policy import HttpPolicy, ProxyPolicy, RateLimitPolicy
        app.configure_http_policy(HttpPolicy(proxy=ProxyPolicy(trusted=["*"])))
        def key(request):
            return request.header("x-forwarded-for")
        app.configure_http_policy(HttpPolicy(
            rate_limit=RateLimitPolicy(limit=5, key=key)
        ))
    """
    assert_flags(source, "untrusted-forwarded-header")
    assert_flags(source, "wildcard-trust-list")


# =============================================================================
# Taint and security-smell rules
# =============================================================================
# The tier's second half. The rules above ask "is this expression dangerous?";
# these ask two further questions that the same AST walk can answer and that no
# expression-level rule can:
#   * What does this *declaration* say?  A signing key is most often a default
#     value on a settings field, not an argument to a call.
#   * What happens on the branch where the check could not be made?  A control
#     that raises on denial and *returns* on "cannot tell" has two exits and one
#     of them is open.
# Same contract as everything above: each rule is tested against the shape it
# catches and against the correct spelling of the same intent.


def test_a_signing_key_declared_as_a_settings_default_is_flagged() -> None:
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
    assert_clean('SESSION_SECRET: str = ""\n', "hardcoded-secret")


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
        app.configure_http_policy(HttpPolicy(proxy=ProxyPolicy(trusted=["*"])))
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_origin_with_credentials_is_flagged() -> None:
    assert_flags(
        """
        app.configure_http_policy(HttpPolicy(
            cors=CorsPolicy(allow_origins=["*"], allow_credentials=True)
        ))
        """,
        "wildcard-trust-list",
    )


def test_a_named_trust_list_is_clean() -> None:
    assert_clean(
        """
        app.configure_http_policy(HttpPolicy(
            proxy=ProxyPolicy(trusted=["10.0.0.0/8"])
        ))
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["trek.example"])
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_origin_without_credentials_is_clean() -> None:
    assert_clean(
        """
        app.configure_http_policy(HttpPolicy(
            cors=CorsPolicy(allow_origins=["*"])
        ))
        """,
        "wildcard-trust-list",
    )


def test_a_wildcard_proxy_trust_does_not_establish_the_boundary() -> None:
    assert_flags(
        """
        app.configure_http_policy(HttpPolicy(proxy=ProxyPolicy(trusted=["*"])))

        @router.get("/sightings")
        async def sightings(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_a_configured_proxy_trust_still_silences_the_forwarded_rule() -> None:
    assert_clean(
        """
        app.configure_http_policy(HttpPolicy(
            proxy=ProxyPolicy(trusted=["10.0.0.0/8"])
        ))

        @router.get("/sightings")
        async def sightings(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_a_double_submit_token_compared_with_equality_is_flagged() -> None:
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
    assert_clean(
        """
        def check(header_token, header_scheme):
            return header_token != header_scheme
        """,
        "timing-unsafe-compare",
    )


def test_a_credential_interpolated_into_a_log_line_is_flagged() -> None:
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
    assert_flags(
        """
        def verify(api_key):
            logger.warning("rejecting api key %s", api_key)
        """,
        "secret-in-log",
    )


def test_a_logged_request_body_is_flagged() -> None:
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
    assert_clean(
        """
        def verify(token):
            logger.warning("the presented token was rejected")
        """,
        "secret-in-log",
    )


def test_a_secret_outside_a_logging_call_is_clean() -> None:
    assert_clean(
        """
        def call(session_token):
            return {"authorization": f"Bearer {session_token}"}
        """,
        "secret-in-log",
    )


def test_an_authorization_check_that_returns_when_undecided_is_flagged() -> None:
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
    assert_clean(
        """
        async def authorise_reserve(request, reserve_id):
            if reserve_id in request.state.principal.reserves:
                return request.state.principal.id
            raise Forbidden("not a member of this reserve")
        """,
        "authz-fail-open",
    )


def test_a_configuration_flag_that_skips_authentication_is_flagged() -> None:
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
    for source in (
        "if settings.REQUIRE_MFA:\n    enforce_mfa(principal)\n",
        "if not settings.TELEMETRY_ENABLED:\n    return\n",
        "limit = 5 if settings.THROTTLE_ENABLED else 500\n",
    ):
        assert_clean(source, "auth-disable-flag")


def test_a_caught_exception_returned_as_the_detail_is_flagged() -> None:
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


def test_a_fetch_of_a_caller_supplied_url_is_flagged() -> None:
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
    assert_clean(
        """
        @router.post("/sightings")
        async def ingest(request):
            body = await request.json()
            return body.get("station")
        """,
        "outbound-url-from-request",
    )


def test_a_cookie_flag_conditioned_on_the_environment_is_flagged() -> None:
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


def test_a_path_exemption_matched_by_substring_is_flagged() -> None:
    assert_flags(
        """
        def is_exempt(path: str) -> bool:
            return any(route in path for route in EXEMPT_ROUTES)
        """,
        "substring-security-match",
    )


def test_a_policy_string_matched_by_substring_is_flagged() -> None:
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
    for source in (
        'def check(principal):\n    return "admin" in principal.roles\n',
        "def check(scopes):\n    return SCOPE_ADMIN in scopes\n",
        "def check(method, allowed):\n    return method in allowed\n",
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


def test_an_authentication_path_that_falls_back_to_a_second_verifier_is_flagged() -> None:
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


def test_a_declared_body_walked_onto_a_model_is_flagged() -> None:
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
# Every test below exists because `wreath mutant --changed` removed a guard and
# no test objected. A rule's *quiet* half is the half that decides whether it
# survives contact with a real codebase, and an `assert_clean` passes trivially
# against a rule that never fires at all -- so a clean test nobody has falsified
# is not evidence. Each of these names the specific guard it holds down.


def test_a_readable_identifier_under_a_secret_name_is_clean() -> None:
    for source in (
        '_STATE_TOKEN = "_camera_trap_csrf_token"\n',
        'UNUSABLE_PASSWORD = "!never-provisioned"\n',
        '_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"\n',
        'SESSION_SECRET_HEADER = "x-camera-trap-session"\n',
    ):
        assert_clean(source, "hardcoded-secret")


def test_a_key_needs_both_letters_and_digits() -> None:
    assert_clean('SIGNING_KEY = "correcthorsebatterystaple"\n', "hardcoded-secret")
    assert_flags('SIGNING_KEY = "correcthorse4batterystaple"\n', "hardcoded-secret")


def test_a_secret_in_a_call_that_is_not_a_logger_is_clean() -> None:
    assert_clean(
        """
        def call(client, session_token):
            return client.post(UPSTREAM, headers={"authorization": f"Bearer {session_token}"})
        """,
        "secret-in-log",
    )


def test_a_secret_logged_as_the_whole_message_is_flagged() -> None:
    assert_flags(
        """
        def verify(api_key):
            logger.info(api_key)
        """,
        "secret-in-log",
    )


def test_a_catalog_is_not_a_logger() -> None:
    assert_clean(
        """
        def record(catalog, api_key):
            catalog.info(f"registered {api_key}")
        """,
        "secret-in-log",
    )


def test_the_strong_secret_vocabulary_is_reachable_on_its_own() -> None:
    assert_flags(
        """
        def sign(hmac_value):
            logger.debug(f"computed {hmac_value}")
        """,
        "secret-in-log",
    )


def test_a_mapping_lookup_with_a_tainted_key_is_clean() -> None:
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
    assert_clean(
        """
        app.configure_http_policy(HttpPolicy(
            cors=CorsPolicy(allow_origins=["*"], allow_credentials=False)
        ))
        """,
        "wildcard-trust-list",
    )


def test_only_proxy_headers_middleware_establishes_the_boundary() -> None:
    assert_flags(
        """
        app.configure_http_policy(HttpPolicy(
            trusted_host=TrustedHostPolicy(trusted=["trek.example"])
        ))

        @router.get("/herds")
        async def herds(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_two_secret_names_sharing_a_provenance_are_clean() -> None:
    assert_clean(
        """
        def check(header_token, header_signature):
            return header_token != header_signature
        """,
        "timing-unsafe-compare",
    )


def test_two_secret_names_with_no_provenance_are_clean() -> None:
    assert_clean(
        """
        def check(token, signature):
            return token != signature
        """,
        "timing-unsafe-compare",
    )


def test_a_refusing_function_returning_on_an_ordinary_condition_is_clean() -> None:
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
    assert_clean(
        """
        def is_exempt(scopes: list[str]) -> bool:
            return "admin" in scopes
        """,
        "substring-security-match",
    )


def test_a_syntax_fragment_searched_in_a_path_is_clean() -> None:
    for source in (
        'def register(path: str):\n    return "{" not in path\n',
        'def register(path: str):\n    return ":path}" in path\n',
        'def register(path: str):\n    return "\\\\" in path\n',
    ):
        assert_clean(source, "substring-security-match")


def test_a_debug_gated_disclosure_is_clean() -> None:
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
    assert_flags(
        """
        def check(supplied, stored):
            return supplied.password == stored.password
        """,
        "timing-unsafe-compare",
    )


def test_one_operand_with_a_provenance_is_not_a_pair() -> None:
    assert_clean(
        """
        def check(header_token, signature):
            return header_token != signature
        """,
        "timing-unsafe-compare",
    )


def test_a_key_like_literal_under_an_ordinary_name_is_clean() -> None:
    assert_clean('SIGHTING_FIXTURE = "a1b2c3d4e5f6a7b8c9"\n', "hardcoded-secret")


def test_a_declared_secret_in_bytes_is_flagged() -> None:
    assert_flags('SIGNING_KEY = b"8c1f0a77e5b3d942ac6e10bf35d8724e"\n', "hardcoded-secret")


def test_an_all_digit_literal_is_not_a_key() -> None:
    assert_clean('TOKEN_EPOCH = "20260804120000000000"\n', "hardcoded-secret")


def test_a_security_flag_decided_by_the_request_is_clean() -> None:
    assert_clean(
        """
        def cookie_for(request):
            secure = True if request.url.scheme == "https" else False
            return secure
        """,
        "env-conditional-security",
    )


def test_a_ternary_security_flag_is_flagged_on_its_own() -> None:
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
    assert_clean(
        """
        app.add_global_middleware(
            CorsPolicy(
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
    assert_flags(
        """
        policy: str = load_policy()

        def may_write(action):
            return action in policy
        """,
        "substring-security-match",
    )


def test_a_secret_named_template_is_not_a_secret_value() -> None:
    assert_clean(
        """
        SECRET_ROTATION_TEMPLATE = "rotated the key for station {}"

        def announce(station_id):
            logger.info(SECRET_ROTATION_TEMPLATE.format(station_id))
        """,
        "secret-in-log",
    )


def test_a_secret_in_the_extra_mapping_is_flagged() -> None:
    assert_flags(
        """
        def verify(session_token):
            logger.info("verified", extra={"token": session_token})
        """,
        "secret-in-log",
    )


def test_an_inline_client_with_a_tainted_url_is_flagged() -> None:
    assert_flags(
        """
        @router.post("/stations/register")
        async def register(request, callback: str):
            return await httpx.AsyncClient().get(callback)
        """,
        "outbound-url-from-request",
    )


def test_only_a_trusted_keyword_establishes_the_boundary() -> None:
    assert_flags(
        """
        app.add_global_middleware(
            ProxyPolicy(trust_host=False, trusted=["*"])
        )

        @router.get("/herds")
        async def herds(request):
            return request.headers.get("x-forwarded-for")
        """,
        "untrusted-forwarded-header",
    )


def test_a_refusal_raised_as_a_bare_class_still_counts() -> None:
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
    assert_clean(
        """
        policy: Policy = load_policy()

        def may_write(action):
            return action in policy
        """,
        "substring-security-match",
    )


def test_a_tainted_value_on_a_non_request_method_is_clean() -> None:
    assert_clean(
        """
        @router.post("/stations/register")
        async def register(request, client, callback: str):
            client.set_base_url(callback)
        """,
        "outbound-url-from-request",
    )


def test_a_tainted_query_parameter_on_a_safe_url_is_clean() -> None:
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
    assert_clean(
        """
        app.add_global_middleware(
            CorsPolicy(allow_origins=["*"], allow_private_network=True)
        )
        """,
        "wildcard-trust-list",
    )


def test_both_operands_must_be_secret_named_to_pair() -> None:
    assert_clean(
        """
        def check(header_token, cookie_value):
            return header_token != cookie_value
        """,
        "timing-unsafe-compare",
    )


def test_a_bare_non_refusal_class_is_read_without_crashing() -> None:
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
    assert_clean(
        """
        app.add_global_middleware(
            CorsPolicy(allow_origins=["*"], allow_credentials=credentialed())
        )
        """,
        "wildcard-trust-list",
    )


def test_any_part_of_a_client_chain_names_the_receiver() -> None:
    assert_flags(
        """
        @router.post("/stations/register")
        async def register(request, callback: str):
            return await http.transport.get(callback)
        """,
        "outbound-url-from-request",
    )


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
    assert_clean(
        """
        @router.get("/rate")
        async def rate(request, count: int):
            return {"per": total / count}
        """,
        "path-from-request",
    )


def test_an_operator_that_is_not_a_join_is_clean() -> None:
    assert_clean(
        """
        @router.get("/offset")
        async def offset(request, index: int):
            return {"at": base_path_total - index}
        """,
        "path-from-request",
    )


def test_a_name_assigned_from_something_other_than_path_is_not_a_path() -> None:
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


def test_an_elementwise_comparison_returning_early_is_flagged() -> None:
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


def test_a_random_draw_with_an_encoding_on_the_end_is_flagged() -> None:
    assert_flags(
        """
        def mint() -> str:
            api_secret = random.randbytes(16).hex()
            return api_secret
        """,
        "weak-randomness",
    )


def test_an_encoded_value_from_elsewhere_is_clean() -> None:
    assert_clean(
        """
        def mint() -> str:
            api_secret = derive(material).hex()
            return api_secret
        """,
        "weak-randomness",
    )


def test_a_development_key_with_no_digits_is_flagged() -> None:
    assert_flags(
        """
        SESSION_SECRET = "northwind-dev-secret"
        """,
        "hardcoded-secret",
    )


def test_the_name_of_the_variable_a_secret_is_read_from_is_clean() -> None:
    assert_clean(
        """
        SESSION_SECRET_VARIABLE = "APP_SESSION_SECRET"
        """,
        "hardcoded-secret",
    )


# The taint model started at the *bound parameter*, so the same handler was an
# ERROR when the value arrived as `q: str` and clean when it arrived off
# `request` -- which is the more idiomatic of the two spellings and the one a
# reader would reach for. `hardening="block"` is sold as "this application does
# not boot carrying one of these", so a blind source is a blind gate.


@pytest.mark.parametrize(
    "read",
    [
        'request.query_params["q"]',
        "request.path_params['name']",
        'request.cookies["t"]',
        'request.headers["x-tenant"]',
        'request.header("x-tenant")',
    ],
)
def test_sql_built_from_a_request_accessor_is_flagged(read: str) -> None:
    assert_flags(
        f"""
        @router.get("/search")
        async def search(request, session):
            return await session.raw(f"SELECT * FROM t WHERE name = '{{{read}}}'").fetch()
        """,
        "sql-interpolation",
    )


@pytest.mark.parametrize(
    "read",
    [
        'request.query_params["q"]',
        'request.headers["x-tenant"]',
    ],
)
def test_a_request_accessor_taints_the_name_it_is_bound_to(read: str) -> None:
    assert_flags(
        f"""
        @router.get("/search")
        async def search(request, session):
            needle = {read}
            return await session.raw(f"SELECT * FROM t WHERE name = '{{needle}}'").fetch()
        """,
        "sql-interpolation",
    )


def test_a_path_joined_with_a_request_accessor_is_flagged() -> None:
    assert_flags(
        """
        @router.get("/download")
        async def download(request):
            return open(EXPORTS / request.query_params["name"]).read()
        """,
        "path-from-request",
    )


def test_request_state_is_not_a_caller_accessor() -> None:
    assert_clean(
        """
        @router.get("/search")
        async def search(request, session):
            tenant = request.state.tenant
            return await session.raw(f"SELECT * FROM t WHERE tenant = {tenant}").fetch()
        """,
        "sql-interpolation",
    )
