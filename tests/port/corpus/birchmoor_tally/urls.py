"""Birchmoor URL configuration — literal `path()` entries, no `re_path`.

`django.urls.path` names its converters the way Flask does, and three of the
four used here have a wreath target: `int` and `str` and `slug` are a `{name}`
placeholder plus a scalar annotation, and `path` is the one wreath converter,
`{key:path}`. `include()` with a literal prefix is `include_router(prefix=...)`.

The trailing slash is the one behavioural difference worth writing down rather
than dropping: Django's `APPEND_SLASH` redirects `/ranges/1` to `/ranges/1/`,
and wreath's router does not, so a port that strips the slash changes what the
old clients get.
"""

from django.urls import include, path

from . import views

range_patterns = [
    path("", views.search_ranges, name="search_ranges"),
    path("<int:range_id>/", views.read_range, name="read_range"),
    path("<int:range_id>/retire/", views.retire_range, name="retire_range"),
    path("<int:range_id>/tallies/", views.record_tally, name="record_tally"),
    path("<int:range_id>/trace/", views.trace_range, name="trace_range"),
    path("<slug:slug>/", views.read_range_by_slug, name="read_range_by_slug"),
]

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("ranges/", include(range_patterns)),
    path("tallies/<int:tally_id>/weigh/", views.weigh_tally, name="weigh_tally"),
    path("observers/<int:observer_id>/", views.observer_summary, name="observer_summary"),
    path("attachments/<path:key>", views.read_attachment, name="read_attachment"),
    path("legacy/ranges/<int:range_id>/", views.legacy_range, name="legacy_range"),
]
