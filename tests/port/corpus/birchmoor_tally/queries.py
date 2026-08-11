"""Birchmoor deer-count reads — `.objects` chains that are already spelled
like the ormar chains the porter translates.

Every verb below has an exact wreath target, and the porter already knows what
it is: `query_rule()` in `_port/analyzer/queries.py` splits `filter`, `get`,
`create`, `count`, `order_by`, `values` and the rest into `_exact` (translated)
and non-exact (needs-review) by reading the *argument list*. `scan.py:670`
short-circuits that whole machine on `imports.reads_django`, because Django's
`objects` may be a manager that hides rows.

`models.py` next door declares no manager, so the premise does not hold here.
The lookups are the same lookups: `__gte` is `>=`, `__in` is `.in_(...)`,
`__icontains` is `.ilike("%x%")`, `__isnull` is `.is_null()`.

The lookups that genuinely need a decision — a relation span, an aggregate, an
`exclude` over a nullable column — are in `foreign/ironwood_tally/`.
"""

from .models import Observer, Range, Tally


def all_ranges():
    return list(Range.objects.all())


def ranges_named(name):
    return list(Range.objects.filter(name=name))


def live_ranges():
    return list(Range.objects.filter(retired=False))


def tallies_since(recorded_at):
    return list(Tally.objects.filter(recorded_at__gte=recorded_at))


def tallies_for_species(species_list):
    return list(Tally.objects.filter(species__in=species_list))


def tallies_missing_weather():
    return list(Tally.objects.filter(weather__isnull=True))


def observers_matching(fragment):
    return list(Observer.objects.filter(display_name__icontains=fragment))


def observers_starting(prefix):
    return list(Observer.objects.filter(email__startswith=prefix))


def range_by_id(range_id):
    return Range.objects.get(id=range_id)


def range_by_slug(slug):
    return Range.objects.get(slug=slug)


def observer_or_none(email):
    return Observer.objects.filter(email=email).first()


def count_tallies(species):
    return Tally.objects.filter(species=species).count()


def any_tallies(range_id):
    return Tally.objects.filter(range_id=range_id).exists()


def record_tally(range_id, observer_id, species, counted, recorded_at):
    return Tally.objects.create(
        range_id=range_id,
        observer_id=observer_id,
        species=species,
        counted=counted,
        recorded_at=recorded_at,
    )


def ranges_by_name():
    return list(Range.objects.order_by("name"))


def newest_tallies():
    return list(Tally.objects.order_by("-recorded_at"))


def tally_page(page, size):
    return list(Tally.objects.order_by("-recorded_at")[(page - 1) * size : page * size])


def tally_names():
    return list(Tally.objects.values("species", "counted"))


def species_column():
    return list(Tally.objects.values_list("species", flat=True))


def tallies_with_observer():
    return list(Tally.objects.select_related("observer"))


def ranges_with_tallies():
    return list(Range.objects.prefetch_related("tallies"))


def retire_range(range_id):
    return Range.objects.filter(id=range_id).update(retired=True)


def drop_tallies(range_id):
    return Tally.objects.filter(range_id=range_id).delete()


def ensure_observer(email, display_name):
    return Observer.objects.get_or_create(email=email, display_name=display_name)
