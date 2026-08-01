"""The precision grid, asserted against the Cedar engine directly.

This is the example's sharpest argument, so it gets the sharpest tests, and they
run with no database and no HTTP -- a policy edit that widens access fails in
milliseconds on every run, including in CI, where there is no PostgreSQL.

Twelve cells: three protection tiers by four principals. The table below is the
one the policy module documents in prose, written out as data so the two cannot
drift silently. If somebody widens a rule, this is what goes red, and it reads
as the same table the docstring shows.
"""

from __future__ import annotations

import pytest
from tracking.models import PROTECTIONS
from tracking.place import APPROXIMATE, COARSE, EXACT, GRADES
from tracking.policies import LOCATE, ROLES, precision_for, precision_grid

from wreath.auth import Identity

#: Rows are protection tiers, columns are principals. `None` means the position
#: is absent from the response -- not null, not zeroed.
GRID: dict[tuple[str, str], object] = {
    ("open", "ranger"): EXACT,
    ("open", "partner"): EXACT,
    ("open", "volunteer"): EXACT,
    ("open", "public"): EXACT,
    ("sensitive", "ranger"): EXACT,
    ("sensitive", "partner"): COARSE,
    ("sensitive", "volunteer"): APPROXIMATE,
    ("sensitive", "public"): None,
    ("restricted", "ranger"): EXACT,
    ("restricted", "partner"): None,
    ("restricted", "volunteer"): None,
    ("restricted", "public"): None,
}


def who(role: str | None, **claims: object) -> Identity | None:
    """An identity of the shape the session backend produces on sign-in.

    `None` is the public: not a role with no permissions, but the absence of a
    sign-in, which `tracking.policies` turns into its own Cedar entity type.
    """
    if role is None:
        return None
    return Identity(
        id=f"{role}-1",
        type="Observer",
        roles=frozenset({role}),
        permissions=frozenset(),
        claims=claims,
    )


@pytest.mark.parametrize(("tier", "principal"), sorted(GRID))
def test_the_precision_grid_is_what_the_policy_says(tier: str, principal: str) -> None:
    """Twelve cells, each an independent test with its own name in the report."""
    identity = who(None if principal == "public" else principal)
    assert precision_for(identity, tier) == GRID[(tier, principal)]


def test_the_four_principals_of_a_sensitive_animal_are_the_whole_argument() -> None:
    """Exact, a kilometre, ten kilometres, absent — from one policy set.

    The headline row, asserted as metres rather than as grade objects, because
    metres is what reaches the wire and a grade renamed without its metres
    changing would still be a silent change to what a reader is shown.
    """
    metres = [
        precision_for(who(principal), "sensitive")
        for principal in ("ranger", "partner", "volunteer")
    ]
    assert [grade.metres for grade in metres] == [0.0, 1_000.0, 10_000.0]
    assert precision_for(None, "sensitive") is None


def test_an_open_animals_position_is_not_a_secret_from_anyone() -> None:
    """The rule is about the animal, not about the badge.

    Without this, a policy that simply gave non-rangers a coarse answer for
    everything would pass every other test in this file while making the public
    map -- half the reason the collars are funded -- useless.
    """
    for principal in (*ROLES, None):
        grade = precision_for(who(principal), "open")
        assert grade is EXACT, f"{principal or 'public'} should see an open track exactly"


def test_a_restricted_animal_has_no_ladder_at_all() -> None:
    """Two rhinos, and nobody but a ranger gets a coordinate of any resolution.

    Cedar's default is deny and no statement is written for `restricted` other
    than the ranger's, so this asserts an *absence* is doing the work. A coarse
    answer here would be worse than no answer: it would tell a reader which
    quarter of the conservancy to search, which is exactly the intelligence
    being withheld.
    """
    assert precision_for(who("ranger"), "restricted") is EXACT
    for principal in ("partner", "volunteer", None):
        assert precision_for(who(principal), "restricted") is None


def test_a_forbid_cannot_be_undone_by_a_later_permit() -> None:
    """The standing suspension, which is the reason `forbid` is in the file.

    A suspended *ranger* -- the most privileged principal there is -- loses
    every tier including the open ones. In Cedar `forbid` overrides `permit`
    unconditionally, so no rule anybody adds later can re-admit them. A
    suspension a subsequent permit could defeat is not a suspension.
    """
    active = precision_grid(who("ranger"))
    suspended = precision_grid(who("ranger", suspended=True))
    assert all(grade is EXACT for grade in active.values())
    assert all(grade is None for grade in suspended.values())


def test_the_public_is_a_principal_rather_than_a_refusal() -> None:
    """An unauthenticated reader reaches the engine, and the engine decides.

    The camera-trap example refuses an absent identity *before* consulting
    Cedar, because every route it serves is authenticated. This application
    publishes a map, so the public is a real principal with a real entitlement
    -- and asserting they get the open tier is what proves the decision is the
    policy's rather than an `if` above it.
    """
    assert precision_for(None, "open") is EXACT
    assert precision_for(None, "sensitive") is None


def test_no_role_can_be_given_to_the_public_entity() -> None:
    """`Public` is its own entity type, so no `principal in Role::"..."` reaches it.

    Asserted by construction rather than by string-matching the policy source: a
    reader whose identity carries every role there is gets the ranger's answer,
    and a reader with no identity does not, which is only true if the two are
    different principals rather than one with an empty role set.
    """
    everything = Identity(
        id="someone",
        type="Observer",
        roles=frozenset(ROLES),
        permissions=frozenset(),
        claims={},
    )
    assert precision_for(everything, "restricted") is EXACT
    assert precision_for(None, "restricted") is None


def test_the_grid_is_derived_from_the_engine_rather_than_listed() -> None:
    """`precision_grid` is the policy's answer, not a parallel table.

    A dict from role to metres would be a second copy of the rules that a policy
    edit leaves behind, and the copy that gets left behind is always the one
    enforcement uses. Asserting the batched form agrees with the per-tier
    decision for every principal is what pins it to the engine.
    """
    for principal in (*ROLES, None):
        identity = who(principal)
        assert precision_grid(identity) == {
            tier: precision_for(identity, tier) for tier in PROTECTIONS
        }


def test_the_decision_depends_on_the_tier_and_the_identity_and_nothing_else() -> None:
    """The property that makes one grid per request safe for a page of rows.

    Every handler asks `precision_grid` once and applies the answer to every fix
    on the page. That is only correct if two calls for the same tier and
    identity cannot disagree -- if the decision were a function of the row, a
    batched grid would hand one animal's grade to another's coordinates.
    """
    identity = who("partner")
    for tier in PROTECTIONS:
        answers = {precision_for(identity, tier) for _ in range(25)}
        assert len(answers) == 1


def test_one_action_exists_per_grade_and_they_are_derived() -> None:
    """Adding a grade cannot be done in `place` and forgotten in `policies`.

    `LOCATE` is built from `GRADES`, so this asserts the derivation rather than
    a list -- and it fails loudly if a grade is added without a policy statement
    to go with it, because the new action will be permitted for nobody and the
    grid tests above will show a tier collapsing.
    """
    assert set(LOCATE) == {grade.name for grade in GRADES}
    assert LOCATE["exact"] == "Position::locate_exact"


def test_the_grades_are_ordered_finest_first() -> None:
    """`precision_for` returns the first grade Cedar permits, so order is the ladder.

    If `GRADES` were sorted coarsest-first, a ranger would be handed the 10 km
    answer -- permitted, and the first one asked about -- and every test above
    that names a ranger would still be checking a real Cedar decision. This is
    the assertion that pins the walk's direction.
    """
    assert [grade.metres for grade in GRADES] == sorted(grade.metres for grade in GRADES)
    assert GRADES[0].metres == 0.0
