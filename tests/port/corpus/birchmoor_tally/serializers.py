"""Birchmoor payload shapes — explicit DRF serializers, which are dataclasses.

`serializers.Serializer` with named fields is a declaration of a request or
response body, and wreath has exactly that: a dataclass, with `binding.Field`
carrying the constraints. `Field` is the marker that holds `min_length`,
`max_length`, `pattern`, `ge`, `le` and `description` — `Query` does not, which
is why a constraint belongs on a body field rather than a query parameter.

`required=False` plus `default=` is a dataclass default. `allow_null=True` is
`| None`. `read_only=True` is a field that only appears on the response
dataclass, so a serializer carrying both directions splits into two.

`ModelSerializer` with `fields = "__all__"` is in `foreign/ironwood_tally/`: it
names no fields at all, so nothing static can say what the body is.
"""

from rest_framework import serializers


class RangeSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=64)
    name = serializers.CharField(max_length=200, min_length=1)
    hectares = serializers.DecimalField(max_digits=10, decimal_places=2)
    retired = serializers.BooleanField(default=False)


class ObserverSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    display_name = serializers.CharField(max_length=120)
    notes = serializers.CharField(required=False, allow_null=True)


class TallySerializer(serializers.Serializer):
    species = serializers.CharField(max_length=64)
    counted = serializers.IntegerField(min_value=0)
    confidence = serializers.IntegerField(min_value=0, max_value=100, default=100)
    recorded_at = serializers.DateTimeField()
