from dataclasses import replace
from types import SimpleNamespace

import pytest

from wreath import Wreath
from wreath._auth.requirements import AuthRequirement


async def endpoint() -> str:
    return "ok"


def application():
    app = Wreath(ai_scraping="allow")
    for name in ("first", "second", "third"):
        app.get(f"/{name}")(endpoint)
    return app


def test_unchanged_image_reads_route_source_once():
    app = application()

    class CountedRoutes(type(app._routes)):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    routes = CountedRoutes(app._routes)
    app._routes = routes
    image = app._application_image
    definitions = image.routes()
    for definition in definitions:
        assert image.operation_id(definition, "GET").startswith("get")
        assert image.contract_candidates(definition, "GET") == ()
    assert routes.iterations == 1


def test_operation_lookup_does_not_scan_route_snapshot():
    app = application()
    image = app._application_image
    definitions = image.routes()
    image.operation_ids()

    class NoIterationTuple(tuple):
        def __iter__(self):
            raise AssertionError("operation lookup scanned the route snapshot")

    image._routes = NoIterationTuple(definitions)
    assert image.operation_id(definitions[-1], "GET") == "getThird"


def test_contract_lookup_does_not_scan_requirements():
    app = application()
    image = app._application_image
    definitions = image.routes()
    requirements = image.requirements()

    class NoIterationTuple(tuple):
        def __iter__(self):
            raise AssertionError("contract lookup scanned route requirements")

    image._requirements = NoIterationTuple(requirements)
    assert image.contract_candidates(definitions[-1], "GET") == ()


@pytest.mark.parametrize("replacement", [False, True])
def test_equal_route_replacement_updates_identity(replacement):
    app = application()
    image = app._application_image
    original = image.routes()[0]
    new = replace(original)
    if replacement:
        app._routes = [new, *app._routes[1:]]
    else:
        app._routes[0] = new
    assert image.routes()[0] is new
    assert image.operation_id(new, "GET") == "getFirst"
    with pytest.raises(ValueError, match="outside this application"):
        image.operation_id(original, "GET")


def test_duplicate_identity_uses_first_operation_id():
    app = application()
    definition = app._routes[0]
    app._routes.append(definition)
    image = app._application_image
    operation_ids, _ = image.operation_ids()
    assert image.operation_id(definition, "GET") == operation_ids[0, "GET"]
    assert image.contract_candidates(definition, "GET") == ()


def test_contract_requirements_follow_identity_after_reordering():
    app = application()
    middleware = SimpleNamespace(applies_to=lambda route: route.authenticated)
    public = replace(app._routes[0], middleware=(middleware,))
    private = replace(
        app._routes[1], middleware=(middleware,), requirement=AuthRequirement(authenticated=True)
    )
    app._routes[:] = [public, private]
    image = app._application_image
    assert image.contract_candidates(public, "GET") == ()
    assert image.contract_candidates(private, "GET") == (middleware,)
    app._routes.reverse()
    assert image.contract_candidates(public, "GET") == ()
    assert image.contract_candidates(private, "GET") == (middleware,)


@pytest.mark.parametrize(
    "operation",
    [
        "append",
        "extend",
        "insert",
        "pop",
        "remove",
        "clear",
        "reverse",
        "sort",
        "setitem",
        "setslice",
        "delitem",
        "delslice",
        "iadd",
        "imul",
        "init",
    ],
)
def test_route_mutators_invalidate_all_image_facts(operation):
    app = application()
    image = app._application_image
    previous = image.routes()
    old_bindings = image.binding_specs()
    old_ids, _ = image.operation_ids()
    routes = app._routes
    extra = replace(previous[0], path="/extra")
    if operation == "append":
        routes.append(extra)
    elif operation == "extend":
        routes.extend([extra])
    elif operation == "insert":
        routes.insert(0, extra)
    elif operation == "pop":
        routes.pop()
    elif operation == "remove":
        routes.remove(previous[0])
    elif operation == "clear":
        routes.clear()
    elif operation == "reverse":
        routes.reverse()
    elif operation == "sort":
        routes.sort(key=lambda route: route.path, reverse=True)
    elif operation == "setitem":
        routes[0] = extra
    elif operation == "setslice":
        routes[:] = [extra]
    elif operation == "delitem":
        del routes[0]
    elif operation == "delslice":
        del routes[1:]
    elif operation == "iadd":
        routes += [extra]
    elif operation == "imul":
        routes *= 2
    elif operation == "init":
        routes.__init__([extra])
    assert image.routes() == tuple(routes)
    assert image.routes() is not previous
    assert image.binding_specs() is not old_bindings
    assert image.operation_ids()[0] is not old_ids
    for definition in routes:
        assert image.operation_id(definition, "GET")
        assert image.contract_candidates(definition, "GET") == ()


def test_partially_failed_extend_invalidates_image():
    app = application()
    image = app._application_image
    original = image.routes()
    extra = replace(original[0], path="/extra")

    def broken():
        yield extra
        raise ValueError("broken iterable")

    with pytest.raises(ValueError, match="broken iterable"):
        app._routes.extend(broken())
    assert image.routes() == (*original, extra)
    assert image.operation_id(extra, "GET") == "getExtra"
