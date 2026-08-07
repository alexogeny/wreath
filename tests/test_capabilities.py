"""The capability lookup: what answers a word a reader already knows.

The point of this surface is that somebody arriving with `celery` in their head
is told `wreath.jobs` rather than left to search a page. So the tests here are
mostly about *why* something matched -- a lookup that returns the right row for
the wrong reason will return the wrong row on the next word.
"""

from __future__ import annotations

import json
from pathlib import Path

from wreath._capabilities import index, lookup
from wreath._cli import main
from wreath._devtools.capability_index import render_module
from wreath._devtools.native_lint import repo_root


def _names(term: str) -> list[str]:
    return [match.capability.name for match in lookup(term)]


def test_a_distribution_name_finds_the_subsystem_that_answers_it() -> None:
    matches = lookup("celery")
    assert [match.reason for match in matches] == ["replaces"] * len(matches)
    assert "jobs" in [match.capability.name for match in matches]


def test_one_word_answered_in_several_places_returns_all_of_them() -> None:
    """`redis` is four subsystems here, and naming one of them would be a lie.

    This is the case the command exists for: the reader's word maps to a
    capability wreath spread across locks, jobs, memory and cache, and an
    answer that stops at the first is how somebody reimplements the other three.
    """
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
    """Prose matches are real answers but must rank below a named one.

    `csrf` appears in the middleware sentence and in no `replaces` list, so it
    has to be findable -- but a term that is *both* a distribution name and a
    word in somebody's sentence must not bury the distribution.
    """
    matches = lookup("csrf")
    assert [match.capability.name for match in matches] == ["middleware"]
    assert matches[0].reason == "capability"


def test_a_named_match_outranks_a_prose_match_for_the_same_term() -> None:
    reasons = [match.reason for match in lookup("cache")]
    assert reasons == sorted(reasons, key=["subsystem", "module", "replaces",
                                           "capability"].index)


def test_an_unknown_word_matches_nothing_rather_than_guessing() -> None:
    assert lookup("thumbnail-generation") == ()


def test_the_index_is_not_empty_and_every_row_names_a_guide() -> None:
    rows = index()
    assert len(rows) > 30
    assert all(row.guides for row in rows)


def test_the_shipped_index_matches_the_manifest_it_is_generated_from() -> None:
    """The one gate that keeps this honest.

    The lookup ships inside the wheel, where `docs/agents/manifest.json` does
    not exist, so the data is generated into the package. A generated file with
    no staleness check is a map that rots, which is the failure `wreath-map-lint`
    exists to prevent -- so it is compared byte for byte, and a hand edit fails
    here exactly as a missed regeneration does.
    """
    root = repo_root()
    manifest = json.loads(
        (root / "docs/agents/manifest.json").read_text(encoding="utf-8"))
    generated = Path(root / "src/wreath/_capability_data.py").read_text(encoding="utf-8")
    assert generated == render_module(manifest), (
        "src/wreath/_capability_data.py is stale; regenerate it with "
        "`uv run wreath-map-lint --fix`"
    )


def test_the_command_prints_the_module_and_the_guide(capsys) -> None:
    assert main(["capabilities", "celery"]) == 0
    out = capsys.readouterr().out
    assert "wreath.jobs" in out
    assert "docs/guides/jobs.md" in out


def test_the_command_refuses_an_unknown_word_by_name(capsys) -> None:
    """Exit 1, and say the word back -- a script wants the code, a reader wants
    to see that the term it searched for is the term it typed."""
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
