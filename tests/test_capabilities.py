from __future__ import annotations

import json

from wreath._capabilities import index, lookup
from wreath._cli import main


def _names(term: str) -> list[str]:
    return [match.capability.name for match in lookup(term)]


def test_a_distribution_name_finds_the_subsystem_that_answers_it() -> None:
    matches = lookup("celery")
    assert [match.reason for match in matches] == ["replaces"] * len(matches)
    assert "jobs" in [match.capability.name for match in matches]


def test_one_word_answered_in_several_places_returns_all_of_them() -> None:
    assert set(_names("redis")) >= {"locks", "jobs", "cache"}


def test_a_subsystem_is_found_by_its_own_name() -> None:
    matches = lookup("jobs")
    assert matches[0].capability.name == "jobs"
    assert matches[0].reason == "subsystem"


def test_a_module_is_found_by_its_import_path() -> None:
    matches = lookup("wreath.messaging")
    assert matches[0].capability.name == "jobs"
    assert matches[0].reason == "module"
    assert "wreath.messaging" in matches[0].capability.modules


def test_a_word_in_the_capability_sentence_is_the_weakest_match() -> None:
    matches = lookup("csrf")
    assert [match.capability.name for match in matches] == ["middleware"]
    assert matches[0].reason == "capability"


def test_a_named_match_outranks_a_prose_match_for_the_same_term() -> None:
    reasons = [match.reason for match in lookup("cache")]
    assert reasons == sorted(reasons, key=["subsystem", "module", "replaces", "capability"].index)


def test_an_unknown_word_matches_nothing_rather_than_guessing() -> None:
    assert lookup("thumbnail-generation") == ()


def test_the_index_is_not_empty() -> None:
    rows = index()
    assert len(rows) > 30


def test_the_command_prints_the_module(capsys) -> None:
    assert main(["capabilities", "celery"]) == 0
    out = capsys.readouterr().out
    assert "wreath.jobs" in out


def test_the_command_refuses_an_unknown_word_by_name(capsys) -> None:
    assert main(["capabilities", "thumbnail-generation"]) == 1
    captured = capsys.readouterr()
    assert "thumbnail-generation" in captured.err
    assert "nothing here answers" in captured.err


def test_the_command_with_no_word_lists_every_capability(capsys) -> None:
    assert main(["capabilities"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") >= len(index())


def test_the_json_form_carries_the_reason_each_row_matched(capsys) -> None:
    assert main(["capabilities", "celery", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["term"] == "celery"
    assert {row["reason"] for row in payload["matches"]} == {"replaces"}
    assert "jobs" in {row["name"] for row in payload["matches"]}


def test_the_json_form_without_a_word_lists_every_capability(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["term"] is None
    assert len(payload["matches"]) == len(index())
    assert {row["name"] for row in payload["matches"]} == {
        capability.name for capability in index()
    }
