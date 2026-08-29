from __future__ import annotations

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath.auth import authenticated
from wreath.signatures import (
    PaymentRequired,
    crawler_policy,
    llms_txt,
    robots_disallow,
    robots_txt,
)


def sample_app() -> Wreath:
    app = Wreath()

    @app.get("/")
    async def home() -> dict:
        """The landing page."""
        return {}

    @app.get("/articles/{slug}")
    async def article(slug: str) -> dict:
        """One published article."""
        return {}

    @app.get("/admin/users")
    @authenticated()
    async def users() -> dict:
        """Every user."""
        return {}

    @app.get("/internal/metrics", include_in_schema=False)
    async def metrics() -> dict:
        return {}

    return app


def test_public_and_protected_routes_are_split_by_the_frameworks_own_rule():
    policy = crawler_policy(sample_app())
    assert "/" in policy.allow
    assert "/articles/" in policy.allow
    assert "/admin/users" in policy.disallow
    # include_in_schema=False is not advertised at all, either way.
    assert "/internal/metrics" not in policy.allow
    assert "/internal/metrics" not in policy.disallow


def test_protecting_a_route_moves_it_without_touching_a_second_file():
    before = crawler_policy(sample_app())
    assert "/" in before.allow

    app = Wreath()

    @app.get("/")
    @authenticated()
    async def home() -> dict:
        """The landing page."""
        return {}

    after = crawler_policy(app)
    assert "/" in after.disallow
    assert "/" not in after.allow


def test_a_parameterised_path_is_reduced_to_its_static_prefix():
    assert "/articles/{slug}" not in crawler_policy(sample_app()).allow


def test_robots_txt_lists_disallow_before_allow():
    body = robots_txt(sample_app(), sitemap="https://x/sitemap.xml", crawl_delay=5)
    lines = body.splitlines()
    assert "User-agent: gptbot" in lines
    assert lines.index("User-agent: gptbot") < lines.index("User-agent: *")
    assert lines.index("Disallow: /") < lines.index("User-agent: *")
    assert lines.index("Disallow: /admin/users") < lines.index("Allow: /")
    assert "Crawl-delay: 5" in lines
    assert "Sitemap: https://x/sitemap.xml" in lines
    assert body.endswith("\n")


def test_robots_txt_reflects_ai_scraping_opt_in() -> None:
    body = robots_txt(Wreath(ai_scraping="allow"))
    assert "User-agent: gptbot" not in body
    assert body.startswith("User-agent: *\n")


def test_robots_txt_omits_unspecified_optional_directives():
    body = robots_txt(Wreath())
    assert "Crawl-delay:" not in body
    assert "Sitemap:" not in body


def test_disallow_collapses_to_covering_prefixes():
    app = Wreath()

    @app.get("/admin/a")
    @authenticated()
    async def a() -> dict:
        """A."""
        return {}

    @app.get("/admin/a/b")
    @authenticated()
    async def b() -> dict:
        """B."""
        return {}

    assert robots_disallow(app) == ("/admin/a",)


def test_native_prefix_reduction_agrees_with_an_independent_definition() -> None:
    paths = tuple(
        reversed(
            [
                *(f"/private/{index:04x}" for index in range(1024)),
                "/private/0001/child",
                "/private/0001",
                "/π",
                "/π/child",
                "/z",
            ]
        )
    )
    expected: list[str] = []
    for path in sorted(paths):
        if not any(path.startswith(prefix) for prefix in expected):
            expected.append(path)

    assert _core.minimal_prefixes(paths) == tuple(expected)


def test_native_prefix_reduction_accepts_an_iterable() -> None:
    assert _core.minimal_prefixes(iter(("/api", "/api/private", "/z"))) == (
        "/api",
        "/z",
    )


def test_a_path_with_any_protected_method_is_not_advertised_open():
    app = Wreath()

    @app.get("/x/{item}")
    async def public(item: str) -> dict:
        """Public."""
        return {}

    @app.delete("/x/{item}")
    @authenticated()
    async def protected(item: str) -> dict:
        """Protected."""
        return {}

    policy = crawler_policy(app)
    assert "/x/" in policy.disallow
    assert "/x/" not in policy.allow


def test_llms_txt_lists_only_documented_public_routes():
    body = llms_txt(sample_app(), title="Example", summary="An example service.")
    assert body.startswith("# Example\n")
    assert "> An example service." in body
    assert "The landing page." in body
    assert "One published article." in body
    # Protected, so not offered to a model at all.
    assert "Every user." not in body
    # Undocumented, so there is nothing useful to say about it.
    assert "/internal/metrics" not in body


def test_llms_txt_omits_an_absent_summary_and_undocumented_public_routes():
    app = Wreath()

    @app.get("/undocumented")
    async def undocumented() -> dict:
        return {}

    body = llms_txt(app, title="Example")
    assert body.startswith("# Example\n\n## Endpoints\n")
    assert "/undocumented" not in body


def test_llms_txt_excludes_a_documented_route_hidden_from_schema():
    app = Wreath()

    @app.get("/hidden", include_in_schema=False)
    async def hidden() -> dict:
        """This must stay private."""
        return {}

    assert "/hidden" not in llms_txt(app, title="Example")


def test_payment_required_carries_terms_and_no_settlement():
    error = PaymentRequired(amount="0.002", currency="USD", pay_to="https://pay.example/x")
    assert error.status == 402
    header = error.terms().decode()
    assert header.startswith("http-402;")
    assert 'amount="0.002"' in header
    assert 'currency="USD"' in header
    assert 'pay-to="https://pay.example/x"' in header


async def test_payment_required_renders_as_a_problem_document():
    app = Wreath()

    @app.get("/paid")
    async def paid(request) -> dict:
        raise PaymentRequired(amount="1", currency="USD", pay_to="https://pay.example/x")

    from wreath.testing import TestClient

    async with TestClient(app) as client:
        response = await client.get("/paid")
    assert response.status == 402


def test_the_scheme_is_the_applications_choice():
    error = PaymentRequired(amount="1", currency="USDC", pay_to="0xabc", scheme="x402")
    assert error.terms().decode().startswith("x402;")


@pytest.mark.parametrize("path,expected", [("/a/{x}", "/a/"), ("/{x}", "/"), ("/a", "/a")])
def test_static_prefix_reduction(path, expected):
    from wreath.signatures import _crawlable_path

    assert _crawlable_path(path) == expected


def test_an_explicit_summary_is_preferred_over_the_docstring():
    app = Wreath()

    @app.get("/thing", summary="What a model should read.")
    async def thing() -> dict:
        """A docstring nobody should see here."""
        return {}

    body = llms_txt(app, title="X")
    assert "What a model should read." in body
    assert "A docstring nobody should see here." not in body
