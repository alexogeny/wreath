"""Ironwood admin and DRF wiring — the two Django surfaces with no source form.

* The admin is a whole application, and `wreath.admin` is deliberately not a
  translation of it: `admin.py:1` says it "adds **no new primitive**", so a
  `ModelAdmin` with `list_display`, `list_filter` and `actions` becomes an
  `admin.register(Model, list_columns=(...))` call whose behaviour is not the
  same surface. Custom admin actions and `readonly_fields` callables have no
  target at all.
* `ModelSerializer` with `fields = "__all__"` names no fields. Nothing static
  can say what the body is; the answer is in the model, and which of its
  columns are writable is in the viewset.
* A `DefaultRouter` computes the URL list at startup from the viewset's basename
  and its `@action` methods. The endpoints this service answers on are not
  written here — which is the same reason `foreign.aiohttp.route_dynamic` and
  `foreign.pyramid.config` are refused.
"""

from django.contrib import admin
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.routers import DefaultRouter
from rest_framework.response import Response

from .models import Observer, Sighting


class SightingAdmin(admin.ModelAdmin):
    list_display = ("species", "withdrawn", "seen_at")
    list_filter = ("withdrawn",)
    search_fields = ("species",)
    actions = ("withdraw_selected",)

    def withdraw_selected(self, request, queryset):
        queryset.update(withdrawn=True)


admin.site.register(Sighting, SightingAdmin)
admin.site.register(Observer)


class SightingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sighting
        fields = "__all__"


class SightingViewSet(viewsets.ModelViewSet):
    queryset = Sighting.objects.all()
    serializer_class = SightingSerializer

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        sighting = self.get_object()
        sighting.withdrawn = True
        sighting.save()
        return Response({"withdrawn": sighting.pk})


router = DefaultRouter()
router.register("sightings", SightingViewSet, basename="sighting")
urlpatterns = router.urls
