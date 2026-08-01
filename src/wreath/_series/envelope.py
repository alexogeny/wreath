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

from .compile import CURRENT, PREVIOUS


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
    map against a run the way `series_rows` and `fill` do between
    them — every cell is already a row. What it *does* still owe is the fill
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


def series_rows(declaration: Any, rows: list[Any], *, periods: bool = False) -> Any:
    """Split returned rows into the bucket run and a per-series value map.

    The bucket run comes from the spine, so it is dense and ordered even where
    nothing matched; the map is sparse, and `fill` is what reconciles
    them. A series is keyed by `(key, other)` rather than by `key` alone so
    that a grouping value which is genuinely `NULL` stays distinct from the
    folded remainder, which also carries a `NULL` key.

    With `periods`, the statement carried a discriminator in column 1 and this
    returns one `(buckets, map)` pair per period instead of one overall. Each
    period keeps its own bucket run: the two are legitimately different lengths,
    and a shared run would have to invent buckets for whichever period is
    shorter.
    """
    grouped = declaration.group is not None
    offset = (1 if periods else 0) + (2 if grouped else 0) + 1
    tagged: dict[str, tuple[list[Any], set[Any], dict[tuple[Any, bool], Any]]] = {}
    if periods:
        # Seeded so a period that matched nothing at all still reports an empty
        # run rather than being absent from the payload.
        for name in (CURRENT, PREVIOUS):
            tagged[name] = ([], set(), {})
    else:
        tagged[CURRENT] = ([], set(), {})
    for row in rows:
        bucket = _value(row, 0)
        period = _value(row, 1) if periods else CURRENT
        buckets, seen, found = tagged[period]
        if bucket not in seen:
            seen.add(bucket)
            buckets.append(bucket)
        if grouped:
            base = 2 if periods else 1
            key, other = _value(row, base), bool(_value(row, base + 1))
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
    if periods:
        return {name: (buckets, found) for name, (buckets, _seen, found) in tagged.items()}
    buckets, _seen, found = tagged[CURRENT]
    return buckets, found


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
