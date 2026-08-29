from __future__ import annotations

import pytest

from wreath import Wreath
from wreath._auth.requirements import requirement_for
from wreath.app import MAX_ACCESS_CLAUSES
from wreath.authorization import permissions, roles


def _clauses(*decorators: object, path: str = "/x") -> tuple[int, ...]:
    async def handler(request: object) -> dict[str, int]:
        return {}

    endpoint: object = handler
    for decorate in decorators:
        endpoint = decorate(endpoint)  # type: ignore[operator]
    app = Wreath()
    app.get(path)(endpoint)  # type: ignore[arg-type]
    requirement = requirement_for(endpoint)
    app._compile_capabilities([requirement])
    return app._requirement_clauses(requirement)


def _any_roles(count: int, values_each: int = 4) -> list[object]:
    return [
        roles(*(f"r{index}_{value}" for value in range(values_each)), mode="any")
        for index in range(count)
    ]


@pytest.mark.parametrize("checks,expected", [(1, 4), (2, 16), (3, 64)])
def test_dnf_expansion_is_exact_below_the_ceiling(checks: int, expected: int) -> None:
    assert len(_clauses(*_any_roles(checks))) == expected


def test_expansion_past_the_ceiling_is_refused_at_declaration() -> None:
    with pytest.raises(ValueError, match="clauses"):
        _clauses(*_any_roles(4))


def test_the_refusal_precedes_the_expansion() -> None:
    import time

    start = time.perf_counter()
    with pytest.raises(ValueError):
        _clauses(*_any_roles(12))
    assert time.perf_counter() - start < 1.0


def test_the_refusal_names_the_remedy() -> None:
    with pytest.raises(ValueError) as caught:
        _clauses(*_any_roles(8))
    message = str(caught.value)
    assert "mode='any'" in message
    assert "nested routers" in message
    assert "Cedar" in message


def test_ceiling_is_not_hit_by_ordinary_declarations() -> None:
    assert len(_clauses(roles("admin"))) == 1
    assert len(_clauses(roles("admin", "owner", mode="any"))) == 2
    assert len(_clauses(roles("admin"), permissions("read", "write"))) == 1
    assert len(_clauses(roles("a", "b", mode="any"), permissions("x", "y", mode="any"))) == 4


def test_overlapping_any_checks_collapse_instead_of_multiplying() -> None:
    once = _clauses(roles("a", "b", mode="any"))
    twice = _clauses(roles("a", "b", mode="any"), roles("a", "b", mode="any"))
    assert len(once) == 2
    # {a, b} x {a, b} = {a, a|b, b}: the cross terms merge, so this is 3, not 4.
    assert len(twice) == 3


def test_many_overlapping_checks_stay_under_the_ceiling() -> None:
    repeated = [roles("a", "b", "c", mode="any") for _ in range(40)]
    assert len(_clauses(*repeated)) <= MAX_ACCESS_CLAUSES


def test_all_mode_checks_do_not_multiply() -> None:
    many = [roles(f"r{index}", f"s{index}") for index in range(50)]
    assert len(_clauses(*many)) == 1
