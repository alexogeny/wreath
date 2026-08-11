"""Fernbrake, a bracken-survey portal that reaches the same objects two ways.

Half its URL space is dispatch routes and half is traversal, so half of it is
written down and half is an object graph built from data at request time. Which
view answers depends on the resource *type* and on scan order; who may edit
depends on where in the tree the walk stopped, because the ACL is inherited down
the lineage unless a level resets it.
"""
from pyramid.config import Configurator
from pyramid.security import Allow, Everyone
from pyramid.view import view_config


class Root:
    __name__ = None
    __parent__ = None
    __acl__ = [(Allow, Everyone, "view"), (Allow, "group:surveyors", "edit")]

    def __getitem__(self, key):
        return Compartment(key, parent=self)


class Compartment:
    def __init__(self, name, parent):
        self.__name__ = name
        self.__parent__ = parent

    def __getitem__(self, key):
        return Plot(key, parent=self)


class Plot:
    def __init__(self, name, parent):
        self.__name__ = name
        self.__parent__ = parent


@view_config(context=Plot, permission="view", renderer="json")
def plot_detail(context, request):
    return {
        "plot": context.__name__,
        "compartment": context.__parent__.__name__,
        "units": request.registry.settings["fernbrake.units"],
    }


@view_config(route_name="survey_export", renderer="json")
def survey_export(request):
    return {"season": request.matchdict["season"], "rows": request.params.get("rows")}


def main(global_config, **settings):
    config = Configurator(settings=settings, root_factory=Root)
    config.include("fernbrake_auth")
    config.add_route("survey_export", "/exports/{season}")
    config.add_tween(
        "fernbrake_portal.tweens.timing_tween", over="pyramid.tweens.EXCVIEW"
    )
    config.scan()
    return config.make_wsgi_app()
