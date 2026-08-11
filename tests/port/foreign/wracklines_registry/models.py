"""Strandline surveys: what washes up, where, and when the beach was cleared.

Three of these fields store something wreath has no column type for — a time of
day with no date, a duration, and an address that is either family — so each is
refused by name rather than mapped to the nearest thing that fits. ``tags`` is
not a column at all: Django creates the association table implicitly, and wreath
declares it, so that relation needs a model of its own before either side can
reach the other.
"""
import datetime

from django.contrib import admin
from django.db import models
from django.utils import timezone
from rest_framework import serializers, viewsets


class Tag(models.Model):
    label = models.CharField(max_length=40)

    class Meta:
        db_table = "wrack_tag"


class ClearedManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().exclude(cleared_at=None)


class Strandline(models.Model):
    objects = models.Manager()
    cleared = ClearedManager()

    beach = models.CharField(max_length=64, db_index=True)
    surveyed_on = models.DateField()
    cleared_at = models.TimeField(null=True)
    patrol_length = models.DurationField(null=True)
    logger_address = models.GenericIPAddressField(protocol="IPv4", null=True)
    mass_kg = models.FloatField(default=0.0)
    tags = models.ManyToManyField(Tag, related_name="strandlines")
    warden = models.ForeignKey("Warden", on_delete=models.CASCADE, null=True)

    class Meta:
        db_table = "strandline"

    def save(self, *args, **kwargs):
        self.beach = self.beach.strip().lower()
        if self.pk is None:
            self.full_clean()
        super().save(*args, **kwargs)


class StrandlineSerializer(serializers.ModelSerializer):
    class Meta:
        model = Strandline
        fields = ["beach", "surveyed_on", "cleared_at", "patrol_length", "tags"]


class StrandlineViewSet(viewsets.ModelViewSet):
    serializer_class = StrandlineSerializer

    def get_queryset(self):
        return Strandline._default_manager.filter(beach=self.kwargs["beach"])


def recent_clearances(days=30):
    since = timezone.now() - datetime.timedelta(days=days)
    return Strandline._default_manager.filter(cleared_at__gte=since)


admin.site.register(Strandline)
