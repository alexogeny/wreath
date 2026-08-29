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
    identity = who(None if principal == "public" else principal)
    assert precision_for(identity, tier) == GRID[(tier, principal)]


def test_the_four_principals_of_a_sensitive_animal_are_the_whole_argument() -> None:
    metres = [
        precision_for(who(principal), "sensitive")
        for principal in ("ranger", "partner", "volunteer")
    ]
    assert [grade.metres for grade in metres] == [0.0, 1_000.0, 10_000.0]
    assert precision_for(None, "sensitive") is None


def test_an_open_animals_position_is_not_a_secret_from_anyone() -> None:
    for principal in (*ROLES, None):
        grade = precision_for(who(principal), "open")
        assert grade is EXACT, f"{principal or 'public'} should see an open track exactly"


def test_a_restricted_animal_has_no_ladder_at_all() -> None:
    assert precision_for(who("ranger"), "restricted") is EXACT
    for principal in ("partner", "volunteer", None):
        assert precision_for(who(principal), "restricted") is None


def test_a_forbid_cannot_be_undone_by_a_later_permit() -> None:
    active = precision_grid(who("ranger"))
    suspended = precision_grid(who("ranger", suspended=True))
    assert all(grade is EXACT for grade in active.values())
    assert all(grade is None for grade in suspended.values())


def test_the_public_is_a_principal_rather_than_a_refusal() -> None:
    assert precision_for(None, "open") is EXACT
    assert precision_for(None, "sensitive") is None


def test_no_role_can_be_given_to_the_public_entity() -> None:
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
    for principal in (*ROLES, None):
        identity = who(principal)
        assert precision_grid(identity) == {
            tier: precision_for(identity, tier) for tier in PROTECTIONS
        }


def test_the_decision_depends_on_the_tier_and_the_identity_and_nothing_else() -> None:
    identity = who("partner")
    for tier in PROTECTIONS:
        answers = {precision_for(identity, tier) for _ in range(25)}
        assert len(answers) == 1


def test_one_action_exists_per_grade_and_they_are_derived() -> None:
    assert set(LOCATE) == {grade.name for grade in GRADES}
    assert LOCATE["exact"] == "Position::locate_exact"


def test_the_grades_are_ordered_finest_first() -> None:
    assert [grade.metres for grade in GRADES] == sorted(grade.metres for grade in GRADES)
    assert GRADES[0].metres == 0.0
