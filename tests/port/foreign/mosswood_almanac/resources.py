"""Mosswood hedgerow almanac — the Pyramid half with no paths.

The companion to `corpus/elmshaw_almanac/`. Everything here is refused because
the URL space is not written down:

* traversal — the URL is walked one segment at a time through `__getitem__`, so
  the set of URLs this serves is an object graph built from rows. There is no
  pattern to translate and no static enumeration to take.
* `__acl__` — authorization inherited down the lineage. A decorator on a
  function has no position in a tree.
* `config.scan()` — views attach by scanning for decorators at configuration
  time, so which view wins can depend on scan order.
* `config.include()` — another package's routes, views and tweens land here and
  none of them appear in this file.
* `add_tween(over=..., under=...)` — an ordering constraint, where wreath's
  middleware order is registration order and `priority=`.
* `request.registry.queryUtility(...)` — the implementation is chosen at runtime
  by interface.
* `{year:\\d{4}}` — a regex in the pattern, where wreath's only converter is
  `path`.
"""

from pyramid.config import Configurator
from pyramid.security import Allow, Everyone
from pyramid.view import view_config


class Parish:
    __name__ = None
    __parent__ = None
    __acl__ = [(Allow, Everyone, "view"), (Allow, "group:wardens", "edit")]

    def __init__(self, name, parent):
        self.__name__ = name
        self.__parent__ = parent

    def __getitem__(self, key):
        return Hedge(key, self)


class Hedge:
    def __init__(self, name, parent):
        self.__name__ = name
        self.__parent__ = parent

    def __getitem__(self, key):
        return Survey(key, self)


class Survey:
    __acl__ = [(Allow, "group:wardens", "edit")]

    def __init__(self, name, parent):
        self.__name__ = name
        self.__parent__ = parent


@view_config(context=Hedge, renderer="json", permission="view")
def show_hedge(context, request):
    return {"hedge": context.__name__, "parish": context.__parent__.__name__}


@view_config(context=Survey, renderer="json", permission="edit")
def edit_survey(context, request):
    store = request.registry.queryUtility(ISurveyStore)
    return {"survey": context.__name__, "store": repr(store)}


class ISurveyStore:
    pass


def audit_tween_factory(handler, registry):
    def audit_tween(request):
        return handler(request)

    return audit_tween


def make_app(settings):
    config = Configurator(settings=settings, root_factory=root_factory)
    config.add_route("survey_year", "/surveys/{year:\\d{4}}")
    config.add_tween("mosswood_almanac.resources.audit_tween_factory", over="pyramid.tweens.excview")
    config.include("mosswood_almanac.reporting")
    config.scan()
    return config.make_wsgi_app()


def root_factory(request):
    return Parish("root", None)
