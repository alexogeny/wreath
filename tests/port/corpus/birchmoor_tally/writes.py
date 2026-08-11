"""Birchmoor writes — the same chains as `queries.py`, one import apart.

This module and `queries.py` next door carry the same `.objects` verbs against
the same models. The only difference is that this one imports `django.db`
directly, so `_Imports.reads_django` is true here and false there —
`_port/analyzer/imports.py:69` is literally `return "django" in self.roots`,
and `roots` is *this module's own* imports.

Run the porter over the package and the two files disagree about the same
construct: `queries.py` gets `orm.query.filter_exact` (translated), this one
gets `foreign.django.query` (unsupported). Neither module declares a manager;
`models.py` declares none either. The gate is not measuring what its docstring
says it measures.

`transaction.atomic()` earns its place here on its own: `async with
session.begin()` is the wreath spelling, and a nested `atomic()` is a savepoint
in both — `orm/session.py:1585` names them `wreath_sp_<depth>`.
"""

from django.db import transaction

from .models import Observer, Range, Tally


def live_ranges():
    return list(Range.objects.filter(retired=False))


def tallies_since(recorded_at):
    return list(Tally.objects.filter(recorded_at__gte=recorded_at))


def tallies_for_species(species_list):
    return list(Tally.objects.filter(species__in=species_list))


def range_by_slug(slug):
    return Range.objects.get(slug=slug)


def count_tallies(species):
    return Tally.objects.filter(species=species).count()


def newest_tallies():
    return list(Tally.objects.order_by("-recorded_at"))


def retire_range(range_id):
    with transaction.atomic():
        Range.objects.filter(id=range_id).update(retired=True)
        Tally.objects.filter(range_id=range_id).delete()


def transfer_tallies(from_observer, to_observer):
    with transaction.atomic():
        Observer.objects.get(id=to_observer)
        with transaction.atomic():
            Tally.objects.filter(observer_id=from_observer).update(observer_id=to_observer)
