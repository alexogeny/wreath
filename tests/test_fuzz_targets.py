from __future__ import annotations

import pytest

from wreath._fuzz_targets import INVENTORY, TARGETS, by_name, for_mutation


def test_registry_names_concrete_targets_and_refuses_unknown_names() -> None:
    assert {target.name for target in TARGETS} == {
        "graphql-parser",
        "h2-frames",
        "http-replay-codec",
        "http1-parser",
        "multipart-parser",
        "xml-parser",
    }
    with pytest.raises(ValueError, match="unknown fuzz target 'missing'.*graphql-parser"):
        by_name("missing")


def test_inventory_covers_every_registered_target_with_an_oracle_and_owner() -> None:
    assert {entry.name for entry in INVENTORY} == {target.name for target in TARGETS}
    assert all(entry.boundary and entry.oracle and entry.owner for entry in INVENTORY)
    assert all(entry.native_harness for entry in INVENTORY)
    assert all(any(source.endswith(".c") for source in target.source_files) for target in TARGETS)


def test_http_versioned_seeds_satisfy_target_invariants() -> None:
    target = by_name("http1-parser")
    assert target.seeds
    for seed in target.seeds:
        features = tuple(target.run(seed) or ())
        assert len(features) == len(set(features))


def test_xml_versioned_seeds_satisfy_target_invariants() -> None:
    target = by_name("xml-parser")
    assert target.seeds
    for seed in target.seeds:
        features = tuple(target.run(seed) or ())
        assert len(features) == len(set(features))


def test_http_target_observes_complete_incomplete_and_refused_heads() -> None:
    target = by_name("http1-parser")

    parsed = tuple(target.run(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n") or ())
    incomplete = tuple(target.run(b"GET / HTTP/1.1\r\nHost: x") or ())
    refused = tuple(target.run(b"GET / HTTP/9.9\r\nHost: x\r\n\r\n") or ())

    assert "http:parsed" in parsed
    assert "http:incomplete" in incomplete
    assert "http:refused" in refused


def test_xml_target_distinguishes_profile_refusal_from_success() -> None:
    target = by_name("xml-parser")

    parsed = tuple(target.run(b"<root><child a=\"1\">text</child></root>") or ())
    refused = tuple(target.run(b"<!DOCTYPE root><root/>") or ())

    assert "xml:parsed" in parsed
    assert "xml:refused:doctype" in refused


def test_mutation_metadata_selects_relevant_targets() -> None:
    assert tuple(
        target.name
        for target in for_mutation("src/wreath/xml.py", "guard.remove-raise")
    ) == ("xml-parser",)
    assert tuple(
        target.name
        for target in for_mutation("src/wreath/unrelated.py", "guard.remove-raise")
    ) == ()


@pytest.mark.parametrize(
    ("name", "feature"),
    [
        ("h2-frames", "h2:frames:1"),
        ("graphql-parser", "graphql:parsed"),
        ("multipart-parser", "multipart:parts:1"),
        ("http-replay-codec", "http-replay:decoded"),
    ],
)
def test_added_targets_have_real_seed_corpora_and_reach_success(name: str, feature: str) -> None:
    target = by_name(name)
    assert target.seeds
    assert any(feature in tuple(target.run(seed) or ()) for seed in target.seeds)
