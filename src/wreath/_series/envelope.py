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
    """``(key, {measure: value})`` per returned row.

    The key is ``None`` for an ungrouped declaration, which has exactly one row.
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


def series_rows(
    declaration: Any, rows: list[Any]
) -> tuple[list[Any], dict[tuple[Any, bool], dict[Any, dict[str, Any]]]]:
    """Split returned rows into the bucket run and a per-series value map.

    The bucket run comes from the spine, so it is dense and ordered even where
    nothing matched; the map is sparse, and :func:`fill` is what reconciles
    them. A series is keyed by ``(key, other)`` rather than by ``key`` alone so
    that a grouping value which is genuinely ``NULL`` stays distinct from the
    folded remainder, which also carries a ``NULL`` key.
    """
    grouped = declaration.group is not None
    offset = 3 if grouped else 1
    buckets: list[Any] = []
    seen: set[Any] = set()
    found: dict[tuple[Any, bool], dict[Any, dict[str, Any]]] = {}
    for row in rows:
        bucket = _value(row, 0)
        if bucket not in seen:
            seen.add(bucket)
            buckets.append(bucket)
        if grouped:
            key, other = _value(row, 1), bool(_value(row, 2))
        else:
            key, other = None, False
        values = {
            name: _value(row, offset + index)
            for index, (name, _measure) in enumerate(declaration.measures)
        }
        # A spine row that matched nothing arrives with every measure null and,
        # when grouped, a null key. It establishes the bucket and nothing else;
        # inventing a series from it would put an empty line in the legend.
        if grouped and key is None and not other and all(
            item is None for item in values.values()
        ):
            continue
        found.setdefault((key, other), {})[bucket] = values
    return buckets, found


def fill(declaration: Any, name: str, value: Any) -> Any:
    """What an absent bucket reads as, for one measure.

    An explicit ``.fill(name=...)`` wins. Otherwise the measure's own identity
    element decides: a count or a sum of no rows really is zero, while an
    average, a minimum, or a maximum of no rows is undefined and stays ``None``
    so the renderer draws a gap rather than a plunge to the floor.
    """
    if value is not None:
        return value
    measure = dict(declaration.measures)[name]
    return measure.identity if measure.has_identity else None
