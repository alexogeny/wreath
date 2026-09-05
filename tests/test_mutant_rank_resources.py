import hashlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from wreath._mutant import runner
from wreath._mutant.operators import Candidate


def _original_selection(items, count, digest):
    def rank(item):
        return digest(item[0].encode(), digest_size=16).digest(), item[0]

    groups = {}
    for item in items:
        groups.setdefault(item[1], []).append(item)
    for members in groups.values():
        members.sort(key=rank)
    selected = [
        groups[operator][0]
        for operator in sorted(groups, key=lambda operator: (len(groups[operator]), operator))[
            :count
        ]
    ]
    selected_ids = {item[0] for item in selected}
    remainder = sorted((item for item in items if item[0] not in selected_ids), key=rank)
    selected.extend(remainder[: max(0, count - len(selected))])
    selected.sort(key=rank)
    counts = {operator: len(members) for operator, members in sorted(groups.items())}
    selected_counts = {}
    for _, operator, _ in selected:
        selected_counts[operator] = selected_counts.get(operator, 0) + 1
    return runner.SampleSelection(
        identifiers=tuple(item[0] for item in selected),
        eligible_candidates=len(items),
        candidate_counts_by_operator=counts,
        selected_counts_by_operator=dict(sorted(selected_counts.items())),
        candidate_files=len({item[2] for item in items}),
        selected_files=len({item[2] for item in selected}),
        missing_operators=tuple(operator for operator in counts if operator not in selected_counts),
    )


@pytest.mark.parametrize("families", [(), (32,), (1, 1, 9, 9, 2), (1,) * 20])
@pytest.mark.parametrize("count", [1, 4, 20, 100])
@pytest.mark.parametrize("tied", [False, True])
def test_sampling_matches_original_family_and_hash_order(
    monkeypatch, tmp_path, families, count, tied
):
    source = tmp_path / "fixture.py"
    source.write_text("")
    module = ModuleType("sample_rank_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: module.__name__)
    candidates = []
    items = []
    for family, amount in reversed(list(enumerate(families))):
        operator = f"operator.{family:02}"
        for duplicate in range(amount):
            candidates.append(Candidate(operator, "fixture", 1, ()))
            identifier = f"{operator}@fixture.py:1"
            if duplicate:
                identifier += f"#{duplicate}"
            items.append((identifier, operator, "fixture.py"))
    monkeypatch.setattr(runner, "scan", lambda *args, **kwargs: candidates)
    digest = hashlib.blake2b
    if tied:
        def digest(*args, **kwargs):
            return SimpleNamespace(digest=lambda: b"same-rank")

        monkeypatch.setattr(runner.hashlib, "blake2b", digest)
    assert runner.select_sample([tmp_path], tmp_path, count) == _original_selection(
        items, count, digest
    )


def test_sampling_hashes_each_eligible_candidate_once(monkeypatch, tmp_path):
    source = tmp_path / "fixture.py"
    text = "".join(f"LIMIT_{index} = {index + 1}\n" for index in range(32))
    source.write_text(text)
    module = ModuleType("sample_hash_fixture")
    vars(module).update({f"LIMIT_{index}": index + 1 for index in range(32)})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: module.__name__)
    original = runner.hashlib.blake2b
    hashes = []

    def counted(value, **kwargs):
        hashes.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(runner.hashlib, "blake2b", counted)
    selected = runner.select_sample([tmp_path], tmp_path, 4)
    assert selected.eligible_candidates == 32
    assert len(selected.identifiers) == 4
    assert selected.errors == ()
    assert len(hashes) == len(set(hashes)) == 32


def test_sampling_small_budget_ranks_only_the_chosen_families(monkeypatch, tmp_path):
    source = tmp_path / "fixture.py"
    source.write_text("")
    module = ModuleType("sample_rare_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: module.__name__)
    candidates = [Candidate(f"family.{index:02}", "fixture", 1, ()) for index in range(32)]
    candidates.extend(Candidate("family.00", "common", line, ()) for line in range(2, 10))
    monkeypatch.setattr(runner, "scan", lambda *args, **kwargs: candidates)
    original = runner.hashlib.blake2b
    hashes = []

    def counted(value, **kwargs):
        hashes.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(runner.hashlib, "blake2b", counted)
    selected = runner.select_sample([tmp_path], tmp_path, 4)
    expected = {f"family.{index:02}@fixture.py:1" for index in range(1, 5)}
    assert selected.eligible_candidates == 40
    assert set(selected.identifiers) == expected
    assert len(hashes) == 4
    assert {value.decode() for value in hashes} == expected
