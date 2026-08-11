"""Birchmoor deer-count models — fields only, and no manager anywhere.

The class headers and the fields already translate: `orm.django.model`,
`orm.django.column` and `orm.django.fk` are `translated`, and `emit/django.py`
writes them out. This module exists to establish the *precondition* for
`queries.py` next door: none of these classes declares `objects = ...`, none
overrides `save` or `delete`, and no `Manager` or `QuerySet` subclass exists in
the tree. So `Tally.objects` is every row in `tally`, which is exactly what
`Tally.select()` is — and that is the fact `foreign.django.query` currently
refuses to check.
"""

from django.db import models


class Range(models.Model):
    id = models.BigAutoField(primary_key=True)
    slug = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    hectares = models.DecimalField(max_digits=10, decimal_places=2)
    retired = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "range"


class Observer(models.Model):
    id = models.BigAutoField(primary_key=True)
    email = models.EmailField(max_length=254, unique=True)
    display_name = models.CharField(max_length=120)
    notes = models.TextField(null=True)

    class Meta:
        db_table = "observer"


class Tally(models.Model):
    id = models.BigAutoField(primary_key=True)
    range = models.ForeignKey(Range, on_delete=models.CASCADE, related_name="tallies")
    observer = models.ForeignKey(Observer, on_delete=models.PROTECT, related_name="tallies")
    species = models.CharField(max_length=64, db_index=True)
    counted = models.IntegerField()
    confidence = models.SmallIntegerField(default=100)
    recorded_at = models.DateTimeField()
    weather = models.JSONField(null=True)

    class Meta:
        db_table = "tally"
