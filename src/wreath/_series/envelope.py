"""Turn returned rows into the declared result types.

Two rules live here and they are the ones a hand-rolled chart query usually
gets wrong. **Fill is per measure**, because a count of nothing is zero and an
average of nothing is not — it is undefined, and drawing it as zero puts a
cliff in the chart on every quiet day. And **a series is identified by its key,
never by its position**, because a reader who learned that the north paddock is
the blue line should not be lied to when a filter change drops some other
paddock.
"""

from __future__ import annotations

from typing import Any


def _value(row: Any, index: int) -> Any:
    """One column out of a driver row, by position.

    Positional rather than by name: the statement names its own columns, so
    position is what the two halves already agree on, and a driver that returns
    plain tuples works unchanged.
    """
    return row[index]


def aggregate_rows(declaration: Any, rows: list[Any]) -> list[tuple[Any, dict[str, Any]]]:
    """`(key, {measure: value})` per returned row.

    The key is `None` for an ungrouped declaration, which has exactly one row.
    """
    grouped = declaration.group is not None
    offset = 1 if grouped else 0
    out: list[tuple[Any, dict[str, Any]]] = []
    for row in rows:
        values = {
            name: _value(row, offset + index)
            for index, (name, _measure) in enumerate(declaration.measures)
        }
        out.append((_value(row, 0) if grouped else None, values))
    return out


def cell_rows(declaration: Any, rows: list[Any]) -> list[tuple[int, int, dict[str, Any]]]:
    """`(row, column, {measure: value})` for every cell the spine generated.

    The statement's spine is dense, so this does not have to reconcile a sparse
    map against a run — every cell is already a row. What it *does* still owe
    is the fill
    rule, and it takes it from `fill` rather than restating it: a count
    of nothing is zero and an average of nothing is undefined, on the spatial
    axis for exactly the reason it is on the temporal one.
    """
    # Computed once, exactly as the temporal path does it: `fill` takes the
    # *declared* override as its value and falls back to the measure's identity,
    # so passing a measured value there would read a null row as an override.
    empty = {
        name: fill(declaration, name, declaration.fills.get(name))
        for name, _measure in declaration.measures
    }
    out: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        values = {}
        for index, (name, _measure) in enumerate(declaration.measures):
            found = _value(row, 2 + index)
            values[name] = empty[name] if found is None else found
        out.append((int(_value(row, 0)), int(_value(row, 1)), values))
    return out


def fill(declaration: Any, name: str, value: Any) -> Any:
    """What an absent bucket reads as, for one measure.

    An explicit `.fill(name=...)` wins. Otherwise the measure's own identity
    element decides: a count or a sum of no rows really is zero, while an
    average, a minimum, or a maximum of no rows is undefined and stays `None`
    so the renderer draws a gap rather than a plunge to the floor.
    """
    if value is not None:
        return value
    measure = dict(declaration.measures)[name]
    return measure.identity if measure.has_identity else None
