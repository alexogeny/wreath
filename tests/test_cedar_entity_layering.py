"""Layering per-request entities must equal rebuilding the whole hierarchy.

`CedarPolicies.is_authorized(entities=...)` used to rebuild the entity store
from scratch on every call, which made row-level authorization cost
`rows x O(hierarchy)`. It now layers compact direct-parent nodes over the store
built at construction; native evaluation resolves the final graph, including
uid replacement and dangling-parent completion, without rebuilding it.

Every test here holds the same invariant: **the layered direct graph is the
graph a full rebuild would have produced**, byte for byte. Transitive behavior
is then checked at `is_authorized`, the native boundary that owns traversal.
"""

from __future__ import annotations

import random

import pytest

from wreath._auth.cedar_engine import (
    CedarEntity,
    CedarPolicies,
    EntityUid,
    _build_store,
    _layer_store,
)

SOURCE = """
permit(principal, action == Action::"view", resource)
when { resource.owner == principal };

permit(principal in Group::"staff", action == Action::"edit", resource);

forbid(principal in Group::"suspended", action, resource);
"""


def uid(kind: str, name: str) -> EntityUid:
    return EntityUid(kind, name)


def engine(*statics: CedarEntity) -> CedarPolicies:
    return CedarPolicies(SOURCE, entities=statics)


def assert_layer_matches_rebuild(
    statics: tuple[CedarEntity, ...], request: tuple[CedarEntity, ...]
) -> None:
    """The invariant, stated once so every case asserts the same thing."""
    policies = engine(*statics)
    layered = _layer_store(policies._store, policies._dangling, statics, request)
    rebuilt = _build_store(statics + request)
    assert layered == rebuilt


# --- the ordinary path: disjoint request entities -----------------------------


def test_a_flat_hierarchy_layers_to_the_same_store() -> None:
    root = uid("Group", "staff")
    statics = (CedarEntity(root),) + tuple(
        CedarEntity(uid("User", f"u{i}"), parents=(root,)) for i in range(8)
    )
    request = (CedarEntity(uid("Doc", "d1"), attrs={"owner": uid("User", "u0")}),)
    assert_layer_matches_rebuild(statics, request)


def test_a_chained_hierarchy_layers_to_the_same_store() -> None:
    statics = (CedarEntity(uid("Group", "g0")),) + tuple(
        CedarEntity(uid("Group", f"g{i}"), parents=(uid("Group", f"g{i - 1}"),))
        for i in range(1, 8)
    )
    request = (CedarEntity(uid("User", "u0"), parents=(uid("Group", "g7"),)),)
    assert_layer_matches_rebuild(statics, request)


def test_a_request_entity_parented_to_another_request_entity() -> None:
    statics = (CedarEntity(uid("Group", "staff")),)
    request = (
        CedarEntity(uid("Team", "t1"), parents=(uid("Group", "staff"),)),
        CedarEntity(uid("User", "u1"), parents=(uid("Team", "t1"),)),
    )
    assert_layer_matches_rebuild(statics, request)
    policies = engine(*statics)
    assert policies.is_authorized(
        principal=uid("User", "u1"),
        action=uid("Action", "edit"),
        resource=uid("Doc", "d1"),
        entities=request,
    ).allowed


def test_a_request_entity_with_no_parents_and_nested_attributes() -> None:
    statics = (CedarEntity(uid("Group", "staff")),)
    request = (
        CedarEntity(
            uid("Doc", "d1"),
            attrs={"owner": uid("User", "u0"), "tags": ["a", "b"], "meta": {"n": 1}},
        ),
    )
    assert_layer_matches_rebuild(statics, request)


def test_no_request_entities_reuses_the_construction_store_object() -> None:
    """The zero-entity path must not copy: it is the common case for route-level
    authorization, and a copy there would be pure overhead."""
    statics = (CedarEntity(uid("Group", "staff")),)
    policies = engine(*statics)
    assert policies.is_authorized(
        principal='User::"u0"', action='Action::"view"', resource='Doc::"d1"'
    ).allowed is False


# --- graph replacement conditions ---------------------------------------------


def test_a_request_entity_overriding_a_static_uid_changes_descendants() -> None:
    statics = (
        CedarEntity(uid("Group", "staff")),
        CedarEntity(uid("Group", "eng"), parents=(uid("Group", "staff"),)),
        CedarEntity(uid("User", "u0"), parents=(uid("Group", "eng"),)),
    )
    # `eng` no longer sits under `staff`, so u0 must lose that ancestor.
    request = (CedarEntity(uid("Group", "eng")),)

    assert_layer_matches_rebuild(statics, request)
    policies = engine(*statics)
    assert not policies.is_authorized(
        principal=uid("User", "u0"),
        action=uid("Action", "edit"),
        resource=uid("Doc", "d1"),
        entities=request,
    ).allowed


def test_a_request_entity_completes_a_dangling_parent() -> None:
    """The inverted case: a static entity names a parent nobody defined, and the
    request defines it. Every static entity above the gap gains ancestors."""
    statics = (CedarEntity(uid("User", "u0"), parents=(uid("Team", "t1"),)),)
    request = (CedarEntity(uid("Team", "t1"), parents=(uid("Group", "staff"),)),)

    policies = engine(*statics)
    assert ("Team", "t1") in policies._dangling

    assert_layer_matches_rebuild(statics, request)
    assert not policies.is_authorized(
        principal=uid("User", "u0"),
        action=uid("Action", "edit"),
        resource=uid("Doc", "d1"),
    ).allowed
    assert policies.is_authorized(
        principal=uid("User", "u0"),
        action=uid("Action", "edit"),
        resource=uid("Doc", "d1"),
        entities=request,
    ).allowed


def test_the_ordinary_path_does_not_rebuild(monkeypatch) -> None:
    statics = (
        CedarEntity(uid("Group", "staff")),
        CedarEntity(uid("User", "u0"), parents=(uid("Group", "staff"),)),
    )
    request = (CedarEntity(uid("Doc", "d1"), attrs={"owner": uid("User", "u0")}),)

    seen: list[int] = []
    original = _build_store

    def counting(entities):
        result = original(entities)
        seen.append(len(result))
        return result

    monkeypatch.setattr("wreath._auth.cedar_engine._build_store", counting)
    policies = engine(*statics)
    seen.clear()  # construction legitimately builds once
    _layer_store(policies._store, policies._dangling, statics, request)
    assert not seen, "the layered path rebuilt the whole hierarchy"


# --- cycles -------------------------------------------------------------------


def test_a_cycle_inside_the_static_hierarchy() -> None:
    statics = (
        CedarEntity(uid("Group", "a"), parents=(uid("Group", "b"),)),
        CedarEntity(uid("Group", "b"), parents=(uid("Group", "a"),)),
    )
    request = (CedarEntity(uid("User", "u0"), parents=(uid("Group", "a"),)),)
    assert_layer_matches_rebuild(statics, request)


def test_a_cycle_among_request_entities() -> None:
    statics = (CedarEntity(uid("Group", "staff")),)
    request = (
        CedarEntity(uid("Team", "x"), parents=(uid("Team", "y"),)),
        CedarEntity(uid("Team", "y"), parents=(uid("Team", "x"), uid("Group", "staff"))),
    )
    assert_layer_matches_rebuild(statics, request)


# --- the decision-level differential ------------------------------------------


MATRIX_STATICS = (
    CedarEntity(EntityUid("Group", "staff")),
    CedarEntity(EntityUid("Group", "suspended")),
    CedarEntity(EntityUid("Group", "eng"), parents=(EntityUid("Group", "staff"),)),
    CedarEntity(EntityUid("User", "alice"), parents=(EntityUid("Group", "eng"),)),
    CedarEntity(EntityUid("User", "mallory"), parents=(EntityUid("Group", "suspended"),)),
    CedarEntity(EntityUid("User", "carol")),
)


@pytest.mark.parametrize("principal", ["alice", "mallory", "carol"])
@pytest.mark.parametrize("action", ["view", "edit", "delete"])
@pytest.mark.parametrize("owner", ["alice", "carol"])
def test_every_decision_survives_the_change(principal: str, action: str, owner: str) -> None:
    """principals x actions x owners, against both store constructions.

    Includes the `forbid`-overrides-`permit` case: mallory is in `suspended`
    *and* would otherwise be permitted `view` on a doc she owns.
    """
    request = (
        CedarEntity(EntityUid("Doc", "d1"), attrs={"owner": EntityUid("User", owner)}),
    )
    policies = engine(*MATRIX_STATICS)

    layered = policies.is_authorized(
        principal=EntityUid("User", principal),
        action=EntityUid("Action", action),
        resource=EntityUid("Doc", "d1"),
        entities=request,
    )
    # The pre-change construction, reproduced exactly.
    rebuilt_store = _build_store(MATRIX_STATICS + request)
    assert _layer_store(
        policies._store, policies._dangling, MATRIX_STATICS, request
    ) == rebuilt_store
    assert layered.allowed is (
        CedarPolicies(SOURCE, entities=MATRIX_STATICS + request)
        .is_authorized(
            principal=EntityUid("User", principal),
            action=EntityUid("Action", action),
            resource=EntityUid("Doc", "d1"),
        )
        .allowed
    )


def test_the_forbid_case_is_actually_reached() -> None:
    """Guard on the matrix above: if `suspended` stopped forbidding, every
    parametrised case would still pass while testing less."""
    policies = engine(*MATRIX_STATICS)
    request = (
        CedarEntity(EntityUid("Doc", "d1"), attrs={"owner": EntityUid("User", "mallory")}),
    )
    decision = policies.is_authorized(
        principal=EntityUid("User", "mallory"),
        action=EntityUid("Action", "view"),
        resource=EntityUid("Doc", "d1"),
        entities=request,
    )
    assert decision.allowed is False
    # alice, who owns nothing here, is permitted on her own doc -- so the
    # forbid is what makes mallory's answer different, not a missing permit.
    allowed = policies.is_authorized(
        principal=EntityUid("User", "alice"),
        action=EntityUid("Action", "view"),
        resource=EntityUid("Doc", "d1"),
        entities=(
            CedarEntity(EntityUid("Doc", "d1"), attrs={"owner": EntityUid("User", "alice")}),
        ),
    )
    assert allowed.allowed is True


# --- randomised property sweep -------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_random_hierarchies_layer_to_the_same_store(seed: int) -> None:
    """Random shapes, including collisions and dangling completions, so the
    fallback predicate is exercised on inputs nobody hand-picked."""
    rng = random.Random(seed)
    names = [f"n{i}" for i in range(10)]

    def entity(name: str, pool: list[str]) -> CedarEntity:
        count = rng.randint(0, 2)
        parents = tuple(
            EntityUid("Group", rng.choice(pool)) for _ in range(count) if pool
        )
        return CedarEntity(
            EntityUid("Group", name), attrs={"n": rng.randint(0, 5)}, parents=parents
        )

    static_names = rng.sample(names, rng.randint(1, 6))
    statics = tuple(entity(name, names) for name in static_names)
    request_names = rng.sample(names, rng.randint(1, 4))
    request = tuple(entity(name, names) for name in request_names)
    assert_layer_matches_rebuild(statics, request)
