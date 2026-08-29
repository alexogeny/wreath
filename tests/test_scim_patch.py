from __future__ import annotations

import pytest

from wreath._scim.patch import (
    MAX_OPERATIONS,
    PATCH_OP_URN,
    PatchError,
    apply,
    parse_path,
    replace,
)
from wreath._scim.resources import GROUP, USER


def user_document() -> dict[str, object]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": "7",
        "userName": "alice@example.com",
        "active": True,
        "emails": [{"value": "alice@example.com", "primary": True, "type": "work"}],
        "groups": [],
        "meta": {"resourceType": "User"},
    }


def group_document() -> dict[str, object]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
        "id": "admin",
        "displayName": "admin",
        "members": [{"value": "7", "type": "User"}],
        "meta": {"resourceType": "Group"},
    }


def patch(*operations: dict[str, object]) -> dict[str, object]:
    return {"schemas": [PATCH_OP_URN], "Operations": list(operations)}


def test_a_path_lowercases_and_strips_the_schema_urn() -> None:
    path = parse_path("urn:ietf:params:scim:schemas:core:2.0:User:UserName", shape=USER)
    assert path.attribute == "username"
    assert path.predicate is None
    assert path.sub_attribute is None


def test_a_value_path_carries_its_filter_and_sub_attribute() -> None:
    path = parse_path('members[value eq "7"].display', shape=GROUP)
    assert path.attribute == "members"
    assert path.predicate is not None
    assert path.sub_attribute == "display"


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("", "empty path"),
        ("externalId", "has no attribute named 'externalid'"),
        ('members[value eq "7"', "no closing ']'"),
        ("members[value eq]", "invalid filter"),
        ('members[value eq "7"]junk', "unexpected text after ']'"),
    ],
)
def test_each_malformed_path_has_its_own_refusal(source: str, fragment: str) -> None:
    with pytest.raises(PatchError) as caught:
        parse_path(source, shape=GROUP)
    assert caught.value.scim_type == "invalidPath"
    assert fragment in caught.value.detail


def test_a_body_naming_the_wrong_schema_is_refused() -> None:
    with pytest.raises(PatchError) as caught:
        apply(user_document(), {"schemas": ["nope"], "Operations": []}, shape=USER)
    assert caught.value.scim_type == "invalidSyntax"
    assert PATCH_OP_URN in caught.value.detail


def test_a_body_with_no_operations_is_refused() -> None:
    with pytest.raises(PatchError) as caught:
        apply(user_document(), {"Operations": []}, shape=USER)
    assert "non-empty Operations list" in caught.value.detail


def test_more_operations_than_the_ceiling_are_refused() -> None:
    body = patch(*([{"op": "replace", "path": "active", "value": True}] * (MAX_OPERATIONS + 1)))
    with pytest.raises(PatchError) as caught:
        apply(user_document(), body, shape=USER)
    assert caught.value.scim_type == "invalidValue"
    assert str(MAX_OPERATIONS) in caught.value.detail


def test_an_operation_name_is_case_insensitive() -> None:
    result = apply(
        user_document(), patch({"op": "Replace", "path": "active", "value": False}), shape=USER
    )
    assert result["active"] is False


def test_an_unknown_operation_names_the_three_that_exist() -> None:
    with pytest.raises(PatchError) as caught:
        apply(user_document(), patch({"op": "upsert", "path": "active", "value": True}), shape=USER)
    assert "expected add, remove or replace" in caught.value.detail


def test_a_read_only_attribute_is_refused_with_mutability() -> None:
    with pytest.raises(PatchError) as caught:
        apply(
            user_document(),
            patch({"op": "add", "path": "groups", "value": [{"value": "admin"}]}),
            shape=USER,
        )
    assert caught.value.scim_type == "mutability"
    assert "'groups' is read-only" in caught.value.detail


def test_a_group_display_name_cannot_be_renamed() -> None:
    with pytest.raises(PatchError) as caught:
        apply(
            group_document(),
            patch({"op": "replace", "path": "displayName", "value": "admins"}),
            shape=GROUP,
        )
    assert caught.value.scim_type == "mutability"


def test_a_single_valued_attribute_cannot_be_removed() -> None:
    with pytest.raises(PatchError) as caught:
        apply(user_document(), patch({"op": "remove", "path": "active"}), shape=USER)
    assert caught.value.scim_type == "invalidValue"
    assert "has no meaningful absent state" in caught.value.detail


@pytest.mark.parametrize("path", ['userName[type eq "work"]', "userName.formatted"])
def test_a_single_valued_attribute_refuses_a_filter_and_a_sub_attribute(path: str) -> None:
    with pytest.raises(PatchError) as caught:
        apply(
            user_document(),
            patch({"op": "replace", "path": path, "value": "x"}),
            shape=USER,
        )
    assert caught.value.scim_type == "invalidPath"
    assert "single-valued" in caught.value.detail


def test_a_pathless_replace_applies_each_key_as_a_path() -> None:
    result = apply(
        user_document(),
        patch({"op": "replace", "value": {"active": False, "userName": "b@e.com"}}),
        shape=USER,
    )
    assert result["active"] is False
    assert result["userName"] == "b@e.com"


def test_a_pathless_replace_still_refuses_a_read_only_key() -> None:
    with pytest.raises(PatchError) as caught:
        apply(
            user_document(),
            patch({"op": "replace", "value": {"id": "999"}}),
            shape=USER,
        )
    assert caught.value.scim_type == "mutability"


def test_a_pathless_remove_has_no_target() -> None:
    with pytest.raises(PatchError) as caught:
        apply(user_document(), patch({"op": "remove"}), shape=USER)
    assert caught.value.scim_type == "noTarget"


def test_an_add_without_a_value_is_refused() -> None:
    with pytest.raises(PatchError) as caught:
        apply(user_document(), patch({"op": "add", "path": "active"}), shape=USER)
    assert caught.value.scim_type == "invalidValue"
    assert "needs a value" in caught.value.detail


def test_adding_a_member_that_is_already_there_does_not_duplicate_it() -> None:
    result = apply(
        group_document(),
        patch({"op": "add", "path": "members", "value": [{"value": "7"}]}),
        shape=GROUP,
    )
    assert [member["value"] for member in result["members"]] == ["7"]


def test_a_member_whose_value_is_not_a_string_still_de_duplicates() -> None:
    document = group_document()
    document["members"] = [{"value": 7}]
    result = apply(
        document,
        patch({"op": "add", "path": "members", "value": [{"value": 7}, {"value": "7"}]}),
        shape=GROUP,
    )
    # `7` and `"7"` are different members: a repr-based identity keeps a number
    # and the string of that number apart, where `str()` would merge them.
    assert [member["value"] for member in result["members"]] == [7, "7"]


def test_two_spellings_of_one_member_id_are_one_member() -> None:
    document = group_document()
    document["members"] = [{"value": "AbC"}]
    result = apply(
        document,
        patch({"op": "add", "path": "members", "value": [{"value": "abc"}]}),
        shape=GROUP,
    )
    assert [member["value"] for member in result["members"]] == ["AbC"]


def test_replacing_a_selected_element_replaces_the_whole_element() -> None:
    result = apply(
        group_document(),
        patch({"op": "replace", "path": 'members[value eq "7"]', "value": {"value": "9"}}),
        shape=GROUP,
    )
    assert result["members"] == [{"value": "9"}]


def test_a_bare_string_member_means_the_same_as_an_object() -> None:
    result = apply(
        group_document(),
        patch({"op": "add", "path": "members", "value": "9"}),
        shape=GROUP,
    )
    assert [member["value"] for member in result["members"]] == ["7", "9"]


def test_removing_a_member_that_is_not_there_is_a_no_op() -> None:
    result = apply(
        group_document(),
        patch({"op": "remove", "path": 'members[value eq "404"]'}),
        shape=GROUP,
    )
    assert [member["value"] for member in result["members"]] == ["7"]


def test_removing_a_member_by_filter_removes_exactly_it() -> None:
    result = apply(
        group_document(),
        patch({"op": "remove", "path": 'members[value eq "7"]'}),
        shape=GROUP,
    )
    assert result["members"] == []


def test_removing_a_whole_multi_valued_attribute_clears_it() -> None:
    result = apply(group_document(), patch({"op": "remove", "path": "members"}), shape=GROUP)
    assert result["members"] == []


def test_replacing_through_a_filter_that_matches_nothing_is_refused() -> None:
    with pytest.raises(PatchError) as caught:
        apply(
            group_document(),
            patch({"op": "replace", "path": 'members[value eq "404"].type', "value": "User"}),
            shape=GROUP,
        )
    assert caught.value.scim_type == "noTarget"


def test_replacing_a_whole_multi_valued_attribute_sets_it() -> None:
    result = apply(
        group_document(),
        patch({"op": "replace", "path": "members", "value": [{"value": "9"}]}),
        shape=GROUP,
    )
    assert [member["value"] for member in result["members"]] == ["9"]


def test_a_refusal_part_way_through_applies_nothing() -> None:
    document = user_document()
    with pytest.raises(PatchError):
        apply(
            document,
            patch(
                {"op": "replace", "path": "active", "value": False},
                {"op": "replace", "path": "id", "value": "999"},
            ),
            shape=USER,
        )
    assert document["active"] is True


def test_a_replacement_ignores_read_only_and_unknown_attributes() -> None:
    result = replace(
        user_document(),
        {
            "userName": "new@example.com",
            "id": "999",
            "groups": [{"value": "admin"}],
            "externalId": "from-the-directory",
            "meta": {"resourceType": "Nonsense"},
        },
        shape=USER,
    )
    assert result["userName"] == "new@example.com"
    assert result["id"] == "7"
    assert result["groups"] == []
    assert "externalId" not in result
    assert result["meta"] == {"resourceType": "User"}


def test_a_replacement_leaves_an_omitted_attribute_alone() -> None:
    result = replace(user_document(), {"userName": "new@example.com"}, shape=USER)
    assert result["active"] is True


def test_a_replacement_writes_the_canonical_spelling() -> None:
    result = replace(user_document(), {"username": "new@example.com"}, shape=USER)
    assert result["userName"] == "new@example.com"
    assert "username" not in result


def test_a_replacement_normalises_a_multi_valued_attribute() -> None:
    result = replace(group_document(), {"members": ["9"]}, shape=GROUP)
    assert result["members"] == [{"value": "9"}]


def test_a_replacement_ignores_a_key_that_is_not_a_string() -> None:
    result = replace(user_document(), {7: "surprise", "active": False}, shape=USER)
    assert result["active"] is False
    assert 7 not in result


def test_adding_to_a_multi_valued_attribute_the_document_lacks_starts_it() -> None:
    document = group_document()
    del document["members"]
    result = apply(
        document,
        patch({"op": "add", "path": "members", "value": [{"value": "9"}]}),
        shape=GROUP,
    )
    assert result["members"] == [{"value": "9"}]


def test_a_replacement_body_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(PatchError) as caught:
        replace(user_document(), ["not", "an", "object"], shape=USER)
    assert caught.value.scim_type == "invalidSyntax"
