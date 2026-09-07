from dataclasses import replace

import pytest

from wreath import Wreath
from wreath.request import Request


async def endpoint() -> str:
    return "ok"


async def receive():
    return {"type": "http.request", "body": b""}


def test_warm_named_url_lookup_does_not_compare_every_route_name():
    comparisons = []

    class Name(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            comparisons.append(self)
            return str.__eq__(self, other)

    app = Wreath(ai_scraping="allow")
    for index in range(1000):
        app.get(f"/r/{index}", name=Name(f"route-{index}"))(endpoint)
    assert app.url_path_for("route-999") == "/r/999"
    comparisons.clear()
    assert app.url_path_for("route-999") == "/r/999"
    assert len(comparisons) <= 1


@pytest.mark.parametrize("ordinary_list", [False, True])
def test_named_lookup_preserves_first_duplicate_and_updates_after_mutations(ordinary_list):
    app = Wreath(ai_scraping="allow")
    app.get("/first/{item}", name="item")(endpoint)
    first = app._routes[0]
    second = replace(first, path="/second/{item}")
    app._routes.append(second)
    if ordinary_list:
        app._routes = list(app._routes)
    assert app.url_path_for("item", item="a/b") == "/first/a%2Fb"
    app._routes.reverse()
    assert app.url_path_for("item", item="a/b") == "/second/a%2Fb"
    app._routes[:] = [first]
    assert app._named_route("item") is first
    equal = replace(first)
    app._routes[0] = equal
    assert app._named_route("item") is equal
    app._routes.clear()
    with pytest.raises(KeyError, match="no route named 'item'"):
        app.url_path_for("item", item="a/b")


def test_missing_lookup_refreshes_after_registration():
    app = Wreath(ai_scraping="allow")
    app.get("/old", name="old")(endpoint)
    assert app.url_path_for("old") == "/old"
    with pytest.raises(KeyError, match="no route named 'new'"):
        app.url_path_for("new")
    app.get("/new", name="new")(endpoint)
    assert app.url_path_for("new") == "/new"
    assert app.url_path_for("old") == "/old"


def test_request_reverse_url_uses_updated_named_host_and_path():
    app = Wreath(ai_scraping="allow")
    app.get("/items/{item}", name="item", host="{tenant}.example.test")(endpoint)
    request = Request(
        {"type": "http", "scheme": "https", "root_path": "/api", "headers": []},
        receive,
        app=app,
    )
    assert request.url_for("item", item="a b", tenant="shop") == (
        "https://shop.example.test/api/items/a%20b"
    )
    app._routes[0] = replace(app._routes[0], path="/new/{item}", host="{tenant}.new.test")
    assert request.url_for("item", item="a b", tenant="shop") == (
        "https://shop.new.test/api/new/a%20b"
    )


def test_named_mount_precedes_private_duplicate_route():
    app = Wreath(ai_scraping="allow")
    app.mount("/mounted", Wreath(ai_scraping="allow"), name="child")
    app.get("/route", name="other")(endpoint)
    assert app.url_path_for("other") == "/route"
    app._routes[0] = replace(app._routes[0], name="child")
    assert app.url_path_for("child", path="a b/c") == "/mounted/a%20b/c"
    assert app._host_for("child", {}) is None


def test_unnamed_definition_remains_retrievable_with_none():
    app = Wreath(ai_scraping="allow")
    app.get("/unnamed")(endpoint)
    assert app.url_path_for(None) == "/unnamed"
