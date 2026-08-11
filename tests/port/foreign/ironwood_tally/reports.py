"""Ironwood reports — the `.objects` verbs with no wreath target.

Each of these is refused for a reason in `wreath.orm`, not in the analyzer:

* `aggregate` / `annotate` — the ORM has one aggregate, `Session.count`. There
  is no `group_by`, no `having`, and no `Sum`/`Avg`/`Max` expression;
  `wreath.series` does grouping, and it is a different object with a different
  execution model.
* `distinct` — no builder method exists.
* `exclude` over a nullable column — SQL `NOT (col = v)` is unknown where `col`
  is NULL, and Django's `exclude` also drops those rows. A rewrite to
  `~(Model.col == v)` is only equal on a NOT NULL column.
* `F()` arithmetic in an update — `Session.update_where(query, **values)` takes
  values, not column expressions, so `counted = counted + 1` has no form.
* a `Q` object assembled in a loop — the predicate is not in the source.
* `.raw()` with a hand-written projection — `Session.raw(...).models(Model)`
  exists and is exact, but only when the result names every column exactly
  once; a partial projection raises `MappingError`.
"""

from django.db.models import Count, F, Q, Sum

from .models import Observer, Sighting


def totals_by_species():
    return Sighting.objects.values("species").annotate(total=Count("id"))


def watch_minutes():
    return Sighting.objects.aggregate(Sum("watched_for"))


def distinct_species():
    return Sighting.objects.values_list("species", flat=True).distinct()


def not_withdrawn_named(name):
    return Sighting.objects.exclude(species=name)


def bump_counts(sighting_id):
    return Sighting.objects.filter(id=sighting_id).update(counted=F("counted") + 1)


def matching_any(species_names):
    predicate = Q()
    for name in species_names:
        predicate |= Q(species=name)
    return Sighting.objects.filter(predicate)


def observers_for(species):
    return Observer.objects.filter(sightings__species=species)


def raw_species_totals():
    return Sighting.objects.raw("SELECT species, count(*) AS n FROM sighting GROUP BY species")
