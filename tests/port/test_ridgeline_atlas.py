"""The route options, app shell and auth surface a FastAPI app spells and wreath does not.

`ridgeline_atlas` is the corpus authority for the constructs the tool can only
*report*. Every rule pinned here is needs-review on purpose: each one names a
decision the porter has to make, and none of them is a rewrite the emitter can
do on its own. A `translated` verdict appearing on any of these lines would mean
the tool had promised a rewrite it does not perform.
"""

from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def atlas_root() -> Path:
    return Path(__file__).parent / "corpus" / "ridgeline_atlas"


def _findings(atlas_root: Path, name: str) -> list:
    return [f for f in port.analyze(atlas_root).findings if f.file == name]


def _rules(atlas_root: Path, name: str) -> list[str]:
    return [f.rule_id for f in _findings(atlas_root, name)]


def test_a_lifespan_whose_halves_share_a_name_does_not_split(atlas_root: Path) -> None:
    """`tiles` is made before the yield and closed after, so there is no partition.

    Two hooks are two functions. A name that crosses the yield needs a home
    they share, and choosing that home is the porter's decision -- which is the
    whole difference between `lifespan.ctx` and the translated `lifespan.split`.
    """
    (finding,) = [f for f in _findings(atlas_root, "main.py") if f.construct == "lifespan"]
    assert finding.rule_id == "lifespan.ctx"
    assert finding.tag == port.NEEDS_REVIEW
    assert "tiles" in finding.message


def test_a_timer_middleware_is_custom_rather_than_state_or_exception(
    atlas_root: Path,
) -> None:
    """It neither writes `.state` nor translates an exception, so it is neither rule.

    The three middleware verdicts are told apart by what the class *does*; a
    dispatch that only stamps a response header does none of the two things that
    would move it off the catch-all.
    """
    rules = _rules(atlas_root, "main.py")
    assert rules.count("mw.custom") == 3  # the class, plus both add_middleware calls
    assert "mw.state" not in rules
    assert "mw.exception" not in rules


def test_a_status_code_is_kept_but_a_204_with_a_body_is_a_contradiction(
    atlas_root: Path,
) -> None:
    """`status_code=201` and `status_code=204` are not the same finding.

    The 201 sits on a handler returning a name the analyzer cannot follow, so
    the status carries across and only the return shape needs confirming. The
    204 sits on a handler that returns a dict, which is a contradiction FastAPI
    accepted and someone has to resolve before it is ported.
    """
    by_rule = {f.rule_id: f for f in _findings(atlas_root, "huts.py")}
    assert by_rule["route.status_code"].line == 26
    assert by_rule["route.status_code_empty_body"].line == 32
    assert by_rule["route.status_code_empty_body"].tag == port.NEEDS_REVIEW
    assert "must have no body" in by_rule["route.status_code_empty_body"].message


def test_a_response_class_on_the_decorator_has_no_wreath_slot(atlas_root: Path) -> None:
    (finding,) = [
        f for f in _findings(atlas_root, "huts.py") if f.rule_id == "route.response_class"
    ]
    assert finding.tag == port.NEEDS_REVIEW
    assert "return that response type from the handler" in finding.message


def test_string_rules_on_a_query_parameter_have_nowhere_to_go(atlas_root: Path) -> None:
    """`min_length=`/`max_length=` and `pattern=` are both the same gap.

    Wreath's `Query` marker carries a numeric minimum and maximum and nothing
    else, so neither spelling has a slot -- and the parameter is otherwise an
    ordinary `param.query` the tool would happily call translated.
    """
    findings = [
        f for f in _findings(atlas_root, "huts.py") if f.rule_id == "param.query_strconstraint"
    ]
    assert [f.line for f in findings] == [20, 21]
    assert all(f.tag == port.NEEDS_REVIEW for f in findings)
    assert "param.query" not in _rules(atlas_root, "huts.py")


def test_both_ways_into_an_unmapped_http_exception_are_reported(atlas_root: Path) -> None:
    """A status with no wreath class, and a status that has one but passes `headers=`.

    They are one rule because they need one edit each and the message has to
    name which of the two is in the way; they are two findings because a 503
    and a 429 are different lines with different fixes.
    """
    findings = [f for f in _findings(atlas_root, "huts.py") if f.rule_id == "exc.http_unmapped"]
    assert [f.line for f in findings] == [42, 44]
    assert all(f.tag == port.NEEDS_REVIEW for f in findings)


def test_a_scoped_security_dependency_splits_and_authlib_is_dropped(
    atlas_root: Path,
) -> None:
    rules = _rules(atlas_root, "access.py")
    assert rules.count("auth.security") == 2
    assert "auth.oauth" in rules
    (oauth,) = [f for f in _findings(atlas_root, "access.py") if f.rule_id == "auth.oauth"]
    assert oauth.tag == port.NEEDS_REVIEW
    assert "oauth2_login()" in oauth.message


def test_nothing_the_emitter_leaves_alone_is_called_translated(atlas_root: Path) -> None:
    """The verdicts this fixture is the authority for are all decisions, not rewrites."""
    reported = {f.rule_id for f in port.analyze(atlas_root).findings if f.tag == port.TRANSLATED}
    assert reported.isdisjoint({
        "auth.oauth",
        "auth.security",
        "exc.http_unmapped",
        "lifespan.ctx",
        "mw.custom",
        "param.query_strconstraint",
        "route.response_class",
        "route.status_code_empty_body",
    })
