import itertools

import pytest

from wreath import _replay_adapters
from wreath.postgres import PostgresError


def test_replay_arity_does_not_materialize_declared_range(monkeypatch):
    materialized = []

    def tracked(values):
        if isinstance(values, range):
            materialized.append(len(values))
        return set(values)

    monkeypatch.setattr(_replay_adapters, "set", tracked, raising=False)
    _replay_adapters.refuse_parameter_arity(
        "SELECT " + ", ".join(f"${index}" for index in range(1, 101)), (None,) * 100
    )
    assert materialized == []


def test_small_reference_sets_preserve_postgres_error_precedence():
    for width in range(6):
        for references in itertools.combinations(range(5), width):
            for declared in range(5):
                sql = "SELECT " + (", ".join(f"${index}" for index in references) or "1")
                missing = sorted(set(range(1, declared + 1)) - set(references))
                if 0 in references:
                    expected = "there is no parameter $0"
                elif missing:
                    expected = f"could not determine data type of parameter ${missing[0]}"
                elif references and max(references) > declared:
                    expected = (
                        f"bind message supplies {declared} parameters, "
                        f"but prepared statement requires {max(references)}"
                    )
                else:
                    expected = None
                if expected is None:
                    _replay_adapters.refuse_parameter_arity(sql, (None,) * declared)
                else:
                    with pytest.raises(PostgresError) as caught:
                        _replay_adapters.refuse_parameter_arity(sql, (None,) * declared)
                    assert str(caught.value) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '$0', $1, $1",
        "SELECT $$ $0 $2 $$, $1",
        "SELECT $tag$ $0 $2 $tag$, $1",
        'SELECT "name$0", $1 -- $2\n',
        "SELECT /* $0 /* $2 */ */ $1",
    ],
)
def test_quoted_placeholders_and_repeats_do_not_change_arity(sql):
    _replay_adapters.refuse_parameter_arity(sql, (None,))
