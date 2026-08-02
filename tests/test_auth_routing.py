from __future__ import annotations

import pytest

from wreath._pure.dtrouter import DecisionRouteTable as PureDecisionRouteTable

try:
    from wreath._native import _core
except ImportError:  # pragma: no cover
    _core = None


@pytest.mark.parametrize(
    "table_type",
    [
        pytest.param(PureDecisionRouteTable, id="pure"),
        pytest.param(
            None if _core is None else _core.DecisionRouteTable,
            id="native",
            marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
        ),
    ],
)
def test_decision_router_prunes_routes_above_caller_access(table_type: type) -> None:
    authenticated = 1 << 0
    admin = 1 << 1
    support = 1 << 2
    billing_read = 1 << 3

    table = table_type()
    public_handler = object()
    account_handler = object()
    staff_handler = object()
    billing_handler = object()
    table.add("/public", "GET", public_handler, (0,))
    table.add("/users/{user_id}", "GET", account_handler, (authenticated,))
    table.add(
        "/staff/{section}",
        "GET",
        staff_handler,
        (authenticated | admin, authenticated | support),
    )
    table.add(
        "/billing/{account}",
        "GET",
        billing_handler,
        (authenticated | billing_read,),
    )

    assert table.match("GET", "/public", 0) == (public_handler, None)
    assert table.match("GET", "/users/42", 0) is None
    assert table.match("GET", "/users/42", authenticated) == (
        account_handler,
        {"user_id": "42"},
    )
    assert table.match("GET", "/staff/settings", authenticated) is None
    assert table.match("GET", "/staff/settings", authenticated | support) == (
        staff_handler,
        {"section": "settings"},
    )
    assert table.match("GET", "/billing/acme", authenticated | admin) is None
    assert table.match("GET", "/billing/acme", authenticated | billing_read) == (
        billing_handler,
        {"account": "acme"},
    )


# --- capability summaries across multiple decision levels -------------------
#
# Internal decision nodes carry a summary of the distinct clauses reachable
# below them, not one copy per route beneath the node. Deduplication is only
# sound because pruning asks "is any clause below satisfied", which does not
# depend on clause order or multiplicity. These tables are wide and deep enough
# to force several decision levels, so a summary that lost a clause would show
# up as a route that can no longer be reached.

_DECISION_TABLES = [
    pytest.param(PureDecisionRouteTable, id="pure"),
    pytest.param(
        None if _core is None else _core.DecisionRouteTable,
        id="native",
        marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
    ),
]


def _tenant_table(table_type: type, tenants: int = 12, leaves: int = 12):
    """A multi-level table: shared literals, distinct literals, and a parameter."""
    authenticated = 1 << 0
    control = 1 << 1
    table = table_type()
    handlers: dict[str, object] = {}
    masks: dict[str, int] = {}
    for t in range(tenants):
        tenant_bit = 1 << (2 + t)
        for leaf in range(leaves):
            path = f"/control/tenant-{t}/group-{leaf % 3}/resource-{leaf}/{{item_id}}"
            key = f"{t}:{leaf}"
            handlers[key] = object()
            masks[key] = authenticated | control | tenant_bit
            table.add(path, "GET", handlers[key], (masks[key],))
    return table, handlers, masks


@pytest.mark.parametrize("table_type", _DECISION_TABLES)
def test_inherited_authorization_survives_multiple_decision_levels(table_type) -> None:
    table, handlers, masks = _tenant_table(table_type)
    for key, handler in handlers.items():
        t, leaf = key.split(":")
        path = f"/control/tenant-{t}/group-{int(leaf) % 3}/resource-{leaf}/99"
        assert table.match("GET", path, masks[key]) == (handler, {"item_id": "99"})


@pytest.mark.parametrize("table_type", _DECISION_TABLES)
def test_anonymous_caller_is_pruned_at_every_level(table_type) -> None:
    table, handlers, _masks = _tenant_table(table_type)
    for key in handlers:
        t, leaf = key.split(":")
        path = f"/control/tenant-{t}/group-{int(leaf) % 3}/resource-{leaf}/99"
        assert table.match("GET", path, 0) is None


@pytest.mark.parametrize("table_type", _DECISION_TABLES)
def test_one_tenants_capability_does_not_unlock_another(table_type) -> None:
    """A summary that merged clauses across branches would leak access here."""
    table, handlers, masks = _tenant_table(table_type)
    tenant_0 = masks["0:0"]
    # Tenant 0's mask reaches tenant 0's routes...
    assert table.match("GET", "/control/tenant-0/group-0/resource-0/99", tenant_0) == (
        handlers["0:0"], {"item_id": "99"},
    )
    # ...but not any other tenant's, at any leaf.
    for leaf in range(12):
        path = f"/control/tenant-5/group-{leaf % 3}/resource-{leaf}/99"
        assert table.match("GET", path, tenant_0) is None


@pytest.mark.parametrize("table_type", _DECISION_TABLES)
def test_precedence_and_wildcards_are_unchanged_under_summaries(table_type) -> None:
    authenticated = 1 << 0
    table = table_type()
    literal = object()
    param = object()
    # Same shape, differing only in specificity at the last segment.
    table.add("/a/b/exact", "GET", literal, (authenticated,))
    table.add("/a/b/{name}", "GET", param, (authenticated,))
    # The literal route wins despite both being eligible.
    assert table.match("GET", "/a/b/exact", authenticated) == (literal, None)
    assert table.match("GET", "/a/b/other", authenticated) == (param, {"name": "other"})
    # Both are still pruned for an anonymous caller.
    assert table.match("GET", "/a/b/exact", 0) is None
    assert table.match("GET", "/a/b/other", 0) is None


@pytest.mark.parametrize("table_type", _DECISION_TABLES)
def test_repeated_identical_clauses_still_match(table_type) -> None:
    """Many routes sharing one clause: the summary keeps it exactly once."""
    authenticated = 1 << 0
    table = table_type()
    handlers = []
    for i in range(200):
        handler = object()
        handlers.append(handler)
        table.add(f"/shared/item-{i}", "GET", handler, (authenticated,))
    for i, handler in enumerate(handlers):
        assert table.match("GET", f"/shared/item-{i}", authenticated) == (handler, None)
        assert table.match("GET", f"/shared/item-{i}", 0) is None


@pytest.mark.parametrize("table_type", _DECISION_TABLES)
def test_a_higher_privileged_route_is_skipped_rather_than_ending_the_walk(
    table_type,
) -> None:
    """Two routes at one leaf, one reachable and the more specific one not.

    Every pruning test above is answered by the *node* summary: no clause below
    the node is satisfied, so the walk stops before any route is considered. The
    per-route check only decides anything when a node passes -- some route under
    it is reachable -- and the first candidate in precedence order is not the
    reachable one. Deleting it therefore survived the whole suite while handing
    an admin-only handler to a merely-authenticated caller, complete with a
    `None` params dict that reads exactly like a legitimate literal match.

    Both routes have to be *parameterised*: a path with no `{}` in it is served
    from the static table, which never consults this loop at all, so a literal
    beside a wildcard leaves the wildcard as the only candidate here and the
    check decides nothing. The pair below share a leaf because the wildcard
    route matches the literal first segment as well.
    """
    authenticated = 1 << 0
    admin = 1 << 1
    table = table_type()
    admin_only = object()
    anyone = object()
    table.add("/admin/{page}", "GET", admin_only, (authenticated | admin,))
    table.add("/{tenant}/{page}", "GET", anyone, (authenticated,))

    # An admin gets the specific route.
    assert table.match("GET", "/admin/home", authenticated | admin) == (
        admin_only, {"page": "home"},
    )
    # Everybody else falls through to the tenant route -- a different handler,
    # binding the same request path to different parameters.
    assert table.match("GET", "/admin/home", authenticated) == (
        anyone, {"tenant": "admin", "page": "home"},
    )
    # And an anonymous caller still reaches neither.
    assert table.match("GET", "/admin/home", 0) is None
