"""Drawing register for a surveying practice, addressed by traversal.

There are no route patterns. A URL is walked one segment at a time, each
`__getitem__` returning the next resource, and the view is chosen by the *type*
of whatever the walk arrived at. `/riverside/stage-2/plans/S-101/rev-C` is five
database lookups and no routing table.

Nothing in this file enumerates a single URL this application answers on, and
nothing can: the set is whatever rows exist. An analyzer that reports a route
count here is reporting a number it invented.
"""

from pyramid.security import Allow, Everyone
from pyramid.view import view_config


class Root:
    __name__ = None
    __parent__ = None
    __acl__ = [(Allow, Everyone, "view"), (Allow, "group:staff", "edit")]

    def __init__(self, request):
        self.request = request

    def __getitem__(self, key):
        row = self.request.db.job_by_slug(key)
        if row is None:
            raise KeyError(key)
        return Job(row, self)


class Job:
    def __init__(self, row, parent):
        self.row = row
        self.__name__ = row["slug"]
        self.__parent__ = parent

    def __getitem__(self, key):
        row = self.request_db().stage(self.row["id"], key)
        if row is None:
            raise KeyError(key)
        return Stage(row, self)

    def request_db(self):
        return self.__parent__.request.db


class Stage:
    def __init__(self, row, parent):
        self.row = row
        self.__name__ = row["code"]
        self.__parent__ = parent
        # Resets rather than extends the inherited ACL, so a permission granted
        # further up does not reach past here.
        self.__acl__ = [(Allow, f"job:{row['job_id']}", "view")]

    def __getitem__(self, key):
        return Sheet(key, self)


class Sheet:
    def __init__(self, number, parent):
        self.__name__ = number
        self.__parent__ = parent


@view_config(context=Sheet, permission="view", renderer="json")
def sheet_detail(context, request):
    return {"sheet": context.__name__, "stage": context.__parent__.__name__}


@view_config(context=Stage, permission="view", renderer="json")
def stage_detail(context, request):
    return {"stage": context.__name__}
