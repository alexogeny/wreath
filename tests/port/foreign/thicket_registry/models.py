"""Hedgerow planting records.

`objects` is not every row. The default manager filters the soft-deleted ones
out, so `Planting.objects.filter(...)` carries a predicate that appears at no
call site. Advice to rewrite one of these as a plain select is advice to widen
the query — quietly, and only for rows somebody deleted.

The verbs are spelled the way ormar spells them, which is why a rule written for
ormar fires here and reports the framework wrong.
"""

from django.db import models


class PlantingQuerySet(models.QuerySet):
    def established(self):
        return self.filter(status="established")


class PlantingManager(models.Manager):
    def get_queryset(self):
        return PlantingQuerySet(self.model, using=self._db).filter(is_deleted=False)

    def due_for_survey(self, before):
        return self.get_queryset().filter(next_survey__lte=before)


class Planting(models.Model):
    objects = PlantingManager()
    all_records = models.Manager()

    parcel = models.CharField(max_length=32)
    species = models.CharField(max_length=80, null=True, blank=True)
    planted_on = models.DateField()
    next_survey = models.DateField(null=True, blank=True)
    metres = models.FloatField(default=0.0)
    status = models.CharField(max_length=24, default="new")
    is_deleted = models.BooleanField(default=False)

    class Meta:
        db_table = "planting"

    def save(self, *args, **kwargs):
        if self.metres and self.metres < 0:
            self.metres = 0.0
        super().save(*args, **kwargs)


def survey_backlog(before):
    rows = Planting.objects.due_for_survey(before)
    return [{"parcel": row.parcel, "metres": row.metres} for row in rows]


def record_planting(parcel, species, planted_on):
    planting, created = Planting.objects.get_or_create(
        parcel=parcel, species=species, defaults={"planted_on": planted_on}
    )
    return planting, created
