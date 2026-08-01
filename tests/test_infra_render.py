"""The rendering is the deliverable, so it is compared whole rather than sampled.

Two applications, one with nothing declared and one with every surface stage 1
derives, each rendered against a checked-in golden in `tests/infra_golden/`.
Asserting a phrase at a time proves that one branch printed *something*; only a
whole-output comparison notices a column that stopped lining up, a section that
started printing its empty message while holding rows, or a heading that
silently changed shape.

Regenerate a golden deliberately, never to make a failure go away:

    python -c "import sys, pathlib; sys.path.insert(0, 'tests'); \\
        from _infra_apps import bare; from wreath.infra import infer, render_text; \\
        pathlib.Path('tests/infra_golden/bare.txt').write_text( \\
            render_text(infer(bare(), application='trek.app:app')))"

`{root}` in `rich.txt` stands in for the object store's directory, which is a
temporary one per run; nothing else is substituted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _infra_apps import bare, rich

from wreath.infra import infer, render_json, render_text

GOLDEN = Path(__file__).resolve().parent / "infra_golden"


def _diffable(text: str) -> list[str]:
    return text.splitlines()


def test_an_application_that_declares_nothing_renders_every_absence() -> None:
    plan = infer(bare(), application="trek.app:app")
    assert _diffable(render_text(plan)) == _diffable(
        GOLDEN.joinpath("bare.txt").read_text(encoding="utf-8")
    )


def test_every_surface_renders(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    plan = infer(rich(str(root)), application="trek.app:app")
    expected = GOLDEN.joinpath("rich.txt").read_text(encoding="utf-8")
    assert _diffable(render_text(plan)) == _diffable(expected.replace("{root}", str(root)))


def test_the_rich_plan_round_trips_through_json(tmp_path: Path) -> None:
    plan = infer(rich(str(tmp_path / "scratch")), application="trek.app:app")
    data = json.loads(render_json(plan))
    assert [database["name"] for database in data["databases"]] == ["main", "archive"]
    assert [store["backend"] for store in data["object_stores"]] == ["local", "s3"]
    assert data["listeners"][0]["methods"] == ["GET", "POST"]
    assert "a-secret-that-must-never-be-rendered" not in render_json(plan)


def test_a_virtual_hosted_bucket_says_so_where_a_path_style_one_says_otherwise() -> None:
    """`rich.txt` covers the path-style spelling; this covers the AWS one.

    The two are not cosmetic: an S3-compatible endpoint addressed as though it
    were AWS produces a signature the server rejects, so which one a store uses
    belongs in a plan someone is checking against a bucket.
    """
    app = bare()
    app.objects(
        "cards", backend="s3", bucket="trek-cards", region="eu-west-2",
        access_key="AKIAEXAMPLE", secret_key="secret",
    )
    text = render_text(infer(app, application="trek.app:app"))
    assert (
        "  cards  s3 bucket trek-cards in eu-west-2, virtual-hosted at "
        "trek-cards.s3.eu-west-2.amazonaws.com" in text
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.example.com", "https://api.example.com"),
        ("https://api.example.com:443", "https://api.example.com"),
        ("https://api.example.com:8443", "https://api.example.com:8443"),
        ("http://api.example.com", "http://api.example.com"),
        ("http://api.example.com:80", "http://api.example.com"),
        ("http://api.example.com:8080", "http://api.example.com:8080"),
        # The default port of the *other* scheme is not this scheme's default,
        # and a rule written as though it were would open the wrong one.
        ("https://api.example.com:80", "https://api.example.com:80"),
        ("http://api.example.com:443", "http://api.example.com:443"),
    ],
)
def test_an_origin_keeps_the_port_only_when_it_is_not_the_scheme_default(
    base_url: str, expected: str
) -> None:
    """A firewall rule needs the port; a reader does not need `:443` written out."""
    app = bare()
    app.http_client("api", base_url=base_url)
    (rule,) = infer(app, application="trek.app:app").egress
    assert rule.origin == expected
