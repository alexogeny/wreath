from types import CodeType, SimpleNamespace

import pytest


def test_scope_fixture_names_only_selected_functions():
    from benchmarks.mutant_scope_resources import fixture_source

    source, targets = fixture_source(3, "last")
    assert targets == (("authorize_2", 7),)
    assert source.count("def ordinary_") == 2
    assert source.count("def authorize_") == 1


@pytest.mark.parametrize(("count", "mode"), [(0, "all"), (3, "unknown")])
def test_scope_fixture_refuses_empty_or_unknown_work(count, mode):
    from benchmarks.mutant_scope_resources import fixture_source

    with pytest.raises(ValueError):
        fixture_source(count, mode)


def _plan(value=True, identifier="predicate.always-true@fixture.py:1"):
    compiled = compile(
        f"def authorize_0(value):\n    return {value!r}\n", "fixture.py", "exec", dont_inherit=True
    )
    code = next(item for item in compiled.co_consts if isinstance(item, CodeType))
    mutation = SimpleNamespace(identifier=identifier, patch=SimpleNamespace(code=code))
    return SimpleNamespace(mutations=[mutation], errors=[])


def test_scope_oracle_accepts_independently_compiled_replacement():
    from benchmarks.mutant_scope_resources import verify_plan

    assert verify_plan(_plan(), (("authorize_0", 1),))["mutations"] == 1


@pytest.mark.parametrize("defect", ["empty", "value", "identifier", "error"])
def test_scope_oracle_rejects_incomplete_or_wrong_work(defect):
    from benchmarks.mutant_scope_resources import verify_plan

    plan = _plan(value=defect != "value")
    if defect == "empty":
        plan.mutations.clear()
    elif defect == "identifier":
        plan.mutations[0].identifier = "wrong"
    elif defect == "error":
        plan.errors.append("failed to compile")
    with pytest.raises(ValueError, match="oracle"):
        verify_plan(plan, (("authorize_0", 1),))
