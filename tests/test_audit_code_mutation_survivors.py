from __future__ import annotations

from unittest.mock import patch

import pytest

from wreath._audit.rules import code as audit_code
from wreath._audit.rules.code import scan_source


def findings(source: str, rule_id: str) -> list[str]:
    return [
        finding.message
        for finding in scan_source(source, surface="mutation-survivor.py")
        if finding.rule_id == rule_id
    ]


def test_identifier_suffix_does_not_turn_a_token_id_into_a_secret() -> None:
    assert (
        findings(
            "def matches(token_id, expected):\n    return token_id == expected\n",
            "timing-unsafe-compare",
        )
        == []
    )


def test_bare_reraise_does_not_make_a_fail_open_check_an_authorizer() -> None:
    assert (
        findings(
            """
        def resolve_access(subject):
            if subject is None:
                return
            try:
                load(subject)
            except LookupError:
                raise
        """,
            "authz-fail-open",
        )
        == []
    )


def test_named_refusal_makes_an_undecided_return_fail_open() -> None:
    assert findings(
        """
        def resolve_access(subject):
            if subject is None:
                return
            if not permitted(subject):
                raise Forbidden
        """,
        "authz-fail-open",
    ) == [
        "this leaves the check without deciding, so an unresolved subject is "
        "indistinguishable from a permitted one"
    ]


def test_unpacking_propagates_route_taint_through_tuple_list_and_starred_targets() -> None:
    assert findings(
        """
        @router.get("/fetch")
        async def fetch(request, client, callback: str):
            pair = (callback, "unused")
            [url, *rest] = pair
            return await client.get(url)
        """,
        "outbound-url-from-request",
    ) == ["the destination of this request comes from url"]


def test_starred_target_itself_retains_route_taint() -> None:
    assert findings(
        """
        @router.get("/fetch")
        async def fetch(request, client, callback: str):
            pair = ("unused", callback)
            [ignored, *urls] = pair
            return await client.get(urls)
        """,
        "outbound-url-from-request",
    ) == ["the destination of this request comes from urls"]


def test_annotation_without_a_value_is_a_valid_module_declaration() -> None:
    assert scan_source("SESSION_SECRET: str\n", surface="mutation-survivor.py") == []


def test_module_constant_seed_keeps_random_reproducibility_out_of_security_findings() -> None:
    assert (
        findings(
            "SEED = load_seed()\nrng = random.Random(SEED)\n",
            "weak-randomness",
        )
        == []
    )


@pytest.mark.parametrize("member_source", ["archive.infolist()", "archive.namelist()"])
def test_archive_enumerator_receiver_is_retained_as_member_provenance(member_source: str) -> None:
    assert findings(
        f"""
        members = {member_source}
        for ignored in unrelated:
            destination = root / archive.filename
        """,
        "unsafe-archive-extract",
    ) == ["a destination path is built from an archive member's own name"]


def test_getmembers_call_receiver_is_retained_as_member_provenance() -> None:
    assert findings(
        """
        members = inspect.getmembers(archive)
        for ignored in unrelated:
            destination = root / inspect.name
        """,
        "unsafe-archive-extract",
    ) == ["a destination path is built from an archive member's own name"]


def test_unrelated_calls_do_not_create_archive_member_provenance() -> None:
    assert (
        findings(
            """
        archive.read()
        for ignored in unrelated:
            destination = root / archive.name
        """,
            "unsafe-archive-extract",
        )
        == []
    )


@pytest.mark.parametrize("enumerator", ["infolist", "namelist", "getmembers"])
def test_archive_loop_binding_is_retained_beyond_the_enumerator_loop(enumerator: str) -> None:
    assert findings(
        f"""
        for member in archive.{enumerator}():
            inspect(member)
        for ignored in unrelated:
            destination = root / member.name
        """,
        "unsafe-archive-extract",
    ) == ["a destination path is built from an archive member's own name"]


def test_non_call_archive_loop_does_not_invent_global_member_provenance() -> None:
    assert (
        findings(
            """
        for member in members:
            inspect(member)
        for ignored in unrelated:
            destination = root / member.name
        """,
            "unsafe-archive-extract",
        )
        == []
    )


def test_unrelated_loop_call_does_not_invent_global_member_provenance() -> None:
    assert (
        findings(
            """
        for member in archive.read():
            inspect(member)
        for ignored in unrelated:
            destination = root / member.name
        """,
            "unsafe-archive-extract",
        )
        == []
    )


def test_awaited_dynamic_string_propagates_to_the_sql_sink() -> None:
    assert findings(
        """
        @router.get("/search")
        async def search(request, session, query: str):
            statement = await f"SELECT * FROM records WHERE name = '{query}'"
            return await session.raw(statement).fetch()
        """,
        "sql-interpolation",
    ) == ["SQL passed to .raw() is built by string interpolation"]


def test_taint_propagation_reaches_a_fixed_point() -> None:
    assert findings(
        """
        @router.get("/fetch")
        async def fetch(request, client, callback: str):
            url = intermediate
            intermediate = callback
            return await client.get(url)
        """,
        "outbound-url-from-request",
    ) == ["the destination of this request comes from url"]


def test_taint_propagation_has_no_arbitrary_chain_depth_limit() -> None:
    assert findings(
        """
        @router.get("/fetch")
        async def fetch(request, client, callback: str):
            url = hop_1
            hop_1 = hop_2
            hop_2 = hop_3
            hop_3 = hop_4
            hop_4 = hop_5
            hop_5 = hop_6
            hop_6 = hop_7
            hop_7 = hop_8
            hop_8 = hop_9
            hop_9 = hop_10
            hop_10 = callback
            return await client.get(url)
        """,
        "outbound-url-from-request",
    ) == ["the destination of this request comes from url"]


def test_taint_propagation_stops_walking_at_the_fixed_point() -> None:
    with patch.object(audit_code.ast, "walk", wraps=audit_code.ast.walk) as walk:
        scan_source("value = 1\n", surface="mutation-survivor.py")
    assert walk.call_count <= 8


def test_awaited_request_random_and_origin_values_keep_their_provenance() -> None:
    source = """
        @router.post("/issue")
        async def issue(request):
            body = await request.json()
            rng = random.Random()
            api_secret = rng.choice("abcdef")
            origin = request.header("origin")
            logger.info(body)
            response.headers.append(("access-control-allow-origin", origin))
    """
    result = scan_source(source, surface="mutation-survivor.py")
    assert {finding.rule_id for finding in result} >= {
        "weak-randomness",
        "secret-in-log",
        "cors-reflect-origin",
    }


def test_awaited_random_and_origin_calls_are_unwrapped_before_binding() -> None:
    result = scan_source(
        """
        async def issue(request):
            rng = await random.Random()
            api_secret = rng.choice("abcdef")
            value = await request.header("origin")
            response.headers.append(("access-control-allow-origin", value))
        """,
        surface="mutation-survivor.py",
    )
    assert {finding.rule_id for finding in result} >= {
        "weak-randomness",
        "cors-reflect-origin",
    }


def test_dynamic_string_binding_requires_a_dynamic_expression() -> None:
    flagged = findings(
        """
        @router.get("/search")
        async def search(request, session, query: str):
            statement = f"SELECT * FROM records WHERE name = '{query}'"
            return await session.raw(statement).fetch()
        """,
        "sql-interpolation",
    )
    clean = findings(
        """
        @router.get("/search")
        async def search(request, session, query: str):
            statement = query
            return await session.raw(statement).fetch()
        """,
        "sql-interpolation",
    )
    assert flagged == ["SQL passed to .raw() is built by string interpolation"]
    assert clean == []


def test_ordinary_bound_values_do_not_acquire_security_provenance() -> None:
    assert (
        scan_source(
            """
        value = build_value()
        label = f"constant"
        header = request.header("accept")
        logger.info("ready")
        response.headers.append(("access-control-allow-origin", configured_value))
        """,
            surface="mutation-survivor.py",
        )
        == []
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "rng = random.Random()\napi_secret = rng.choice('abc')\n",
            ["api_secret is drawn from random rather than secrets"],
        ),
        (
            "from random import random\nrng = random()\napi_secret = rng.choice('abc')\n",
            ["api_secret is drawn from random rather than secrets"],
        ),
        (
            "api_secret = random.choice('abc')\n",
            ["api_secret is drawn from random rather than secrets"],
        ),
    ],
)
def test_each_supported_random_binding_spelling_is_detected(
    source: str, expected: list[str]
) -> None:
    assert findings(source, "weak-randomness") == expected


def test_non_random_calls_do_not_taint_a_secret_assignment() -> None:
    assert (
        findings(
            "generator = factory().build()\napi_secret = generator.choice('abc')\n",
            "weak-randomness",
        )
        == []
    )


@pytest.mark.parametrize(
    "source",
    [
        "origin = request.header('origin')\n"
        "response.headers.append(('access-control-allow-origin', origin))\n",
        "origin = request.get('ORIGIN')\n"
        "response.headers.append(('access-control-allow-origin', origin))\n",
    ],
)
def test_origin_reads_are_bound_for_cors_reflection(source: str) -> None:
    assert findings(source, "cors-reflect-origin") == [
        "Access-Control-Allow-Origin is set from the request's own Origin"
    ]


@pytest.mark.parametrize(
    "source",
    [
        "value = 'https://safe.example'\n"
        "response.headers.append(('access-control-allow-origin', value))\n",
        "value = request.lookup('origin')\n"
        "response.headers.append(('access-control-allow-origin', value))\n",
        "value = request.header('accept')\n"
        "response.headers.append(('access-control-allow-origin', value))\n",
    ],
)
def test_non_origin_values_do_not_acquire_origin_provenance(source: str) -> None:
    assert findings(source, "cors-reflect-origin") == []


def test_request_body_logging_reports_bound_and_inline_sources_exactly() -> None:
    bound = findings(
        "body = request.body()\nlogger.info(body)\n",
        "secret-in-log",
    )
    inline = findings("logger.info(request.body())\n", "secret-in-log")
    assert bound == ["body is the caller's own body, which is not known to be free of credentials"]
    assert inline == [
        "a value read off the request is the caller's own body, which is not known "
        "to be free of credentials"
    ]


def test_outbound_url_reports_bound_and_inline_sources_exactly() -> None:
    bound = findings(
        """
        @router.get("/fetch")
        async def fetch(request, client, callback: str):
            return await client.get(callback)
        """,
        "outbound-url-from-request",
    )
    inline = findings(
        "return_value = client.get(request.query_params.get('url'))\n",
        "outbound-url-from-request",
    )
    assert bound == ["the destination of this request comes from callback"]
    assert inline == ["the destination of this request comes from the request"]


@pytest.mark.parametrize(
    "source",
    [
        "SessionPolicy()\n",
        "SessionPolicy(42)\n",
        "SessionPolicy(secret=load_secret())\n",
        "SessionPolicy(secret='')\n",
        "SessionPolicy(secret=42)\n",
        "Widget(label='literal')\n",
    ],
)
def test_non_literal_secret_constructor_forms_are_clean(source: str) -> None:
    assert findings(source, "hardcoded-secret") == []


def test_positional_and_keyword_literal_secrets_are_detected() -> None:
    assert findings("SessionPolicy('literal')\n", "hardcoded-secret") == [
        "SessionPolicy is constructed with a literal signing key"
    ]
    assert findings("Widget(password='literal')\n", "hardcoded-secret") == [
        "password= is a literal"
    ]


@pytest.mark.parametrize(
    "source",
    [
        "Widget(allow_private=True)\n",
        "DestinationPolicy(other=True)\n",
        "DestinationPolicy(allow_private=False)\n",
        "DestinationPolicy(allow_private=setting)\n",
    ],
)
def test_only_literal_true_destination_policy_widenings_are_flagged(source: str) -> None:
    assert findings(source, "ssrf-policy-widened") == []


def test_destination_policy_literal_true_widening_is_flagged() -> None:
    assert findings("DestinationPolicy(allow_private=True)\n", "ssrf-policy-widened") == [
        "DestinationPolicy permits allow_private"
    ]


def test_cors_reflection_accepts_bound_and_origin_named_values_but_not_others() -> None:
    bound = (
        "value = request.header('origin')\nresponse.header('access-control-allow-origin', value)\n"
    )
    named = "response.header('access-control-allow-origin', supplied_origin)\n"
    unrelated = "response.header('access-control-allow-origin', value)\n"
    assert len(findings(bound, "cors-reflect-origin")) == 1
    assert len(findings(named, "cors-reflect-origin")) == 1
    assert findings(unrelated, "cors-reflect-origin") == []


def test_non_header_lookup_cannot_trigger_forwarded_header_rule() -> None:
    assert findings("request.lookup('x-forwarded-for')\n", "untrusted-forwarded-header") == []


def test_nested_random_draws_are_detected_without_tainting_other_chains() -> None:
    assert findings(
        "rng = random.Random()\napi_secret = rng.choice('abc').strip()\n",
        "weak-randomness",
    ) == ["api_secret is drawn from random rather than secrets"]
    assert (
        findings(
            "generator = factory().build()\napi_secret = generator.choice('abc').strip()\n",
            "weak-randomness",
        )
        == []
    )


def test_random_provenance_propagates_through_multiple_bound_generators() -> None:
    assert findings(
        """
        rng = random.Random()
        derived = rng.choice("abc")
        api_secret = derived.transform()
        """,
        "weak-randomness",
    ) == ["api_secret is drawn from random rather than secrets"]


def test_non_random_provenance_does_not_propagate_through_bound_generators() -> None:
    assert (
        findings(
            """
        generator = factory()
        derived = generator.choice("abc")
        api_secret = derived.transform()
        """,
            "weak-randomness",
        )
        == []
    )


def test_timing_rule_requires_equality_and_refuses_literal_dispatch() -> None:
    assert (
        findings(
            "def compare(expected_signature, supplied):\n"
            "    return expected_signature < supplied\n",
            "timing-unsafe-compare",
        )
        == []
    )
    assert (
        findings(
            "def compare(expected_signature):\n    return expected_signature == 'fixed'\n",
            "timing-unsafe-compare",
        )
        == []
    )


def test_case_mapping_requires_a_mapping_method_and_authorization_comparator() -> None:
    assert (
        findings(
            "def compare(user_identity, allowed_role):\n"
            "    return user_identity.strip() == allowed_role\n",
            "case-mapped-authz",
        )
        == []
    )
    assert (
        findings(
            "def compare(user_identity, display_name):\n"
            "    return user_identity.casefold() == display_name\n",
            "case-mapped-authz",
        )
        == []
    )
    assert findings(
        "def compare(user_identity, allowed_role):\n"
        "    return user_identity.casefold() == allowed_role\n",
        "case-mapped-authz",
    ) == [".casefold() is applied before an authorization comparison"]
