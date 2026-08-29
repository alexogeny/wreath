"""Ironwood deer-count models — the Django shapes that stay refused.

The companion to `corpus/birchmoor_tally/`. Same framework, same field
spellings, and none of it is a rewrite. Each stanza is the reason a `.objects`
chain over these classes must *not* be routed through the ormar query rules,
so a fix that reads the manager tree-wide still refuses everything here.

* `ActiveManager` — `get_queryset()` is a predicate on every `.objects` call in
  the codebase and appears at none of them. `Sighting.objects` is not
  `sighting`, so `Sighting.select()` is a widening.
* `save()` override — application logic on the write path. `session.create()`
  and `session.flush()` have no slot for it.
* `ManyToManyField` — an association table Django creates implicitly. Wreath
  declares its tables, so this needs a model before the two sides can relate.
* `post_save` — an ORM signal. `wreath.orm` emits no row events a handler can
  subscribe to at this layer.
* `DurationField` / `TimeField` — `wreath.orm.types` has no `PgType` for either.
"""

from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(withdrawn=False)


class Sighting(models.Model):
    id = models.BigAutoField(primary_key=True)
    species = models.CharField(max_length=64)
    withdrawn = models.BooleanField(default=False)
    watched_for = models.DurationField(null=True)
    seen_at = models.TimeField(null=True)
    observers = models.ManyToManyField("Observer", related_name="sightings")

    objects = ActiveManager()

    class Meta:
        db_table = "sighting"

    def save(self, *args, **kwargs):
        self.species = self.species.strip().lower()
        super().save(*args, **kwargs)


class Observer(models.Model):
    id = models.BigAutoField(primary_key=True)
    display_name = models.CharField(max_length=120)

    class Meta:
        db_table = "ironwood_observer"


@receiver(post_save, sender=Sighting)
def stamp_audit(sender, instance, created, **kwargs):
    raise NotImplementedError("wired up by the app config")
