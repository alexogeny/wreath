"""Capability masks past 64 bits (report 23: R-44).

`Wreath._compile_capabilities` assigns `1 << index` per distinct role and
permission name, so an application with more than 64 of them produces clauses
wider than a machine word. This pins that the whole path -- mask building and
the route table -- stays exact there.
"""

from __future__ import annotations

import pytest

from wreath._native import _core
from wreath.app import Wreath

_TABLES = [_core.PolicyRouteTable]
_BUILDERS = [_core.build_capability_mask]

#: Wider than a machine word on purpose: index 0 is `authenticated`, so a role
#: at index 80 is bit 80.
_WIDE = 100


def _capabilities() -> dict[str, int]:
    names = ["authenticated"] + [f"role:r{index:03d}" for index in range(_WIDE)]
    return {name: 1 << index for index, name in enumerate(sorted(names))}


@pytest.mark.parametrize("build", _BUILDERS)
def test_mask_builder_keeps_bits_past_64(build) -> None:
    capabilities = _capabilities()
    high = "r099"
    mask = build(capabilities, [high], [])
    assert mask & capabilities[f"role:{high}"]
    assert mask.bit_length() > 64


@pytest.mark.parametrize("table_type", _TABLES)
def test_route_table_matches_on_a_bit_past_64(table_type: type) -> None:
    capabilities = _capabilities()
    authenticated = capabilities["authenticated"]
    high = capabilities["role:r099"]
    other = capabilities["role:r000"]
    assert high.bit_length() > 64

    table = table_type()
    handler = object()
    table.add("/wide", "GET", handler, (authenticated | high,))

    # The holder of the high bit gets in; a caller holding every *other* bit
    # must not. A 64-bit truncation makes both of these wrong in the same way.
    assert table.match("GET", "/wide", authenticated | high) == (handler, None)
    assert table.match("GET", "/wide", authenticated | other) is None
    assert table.match("GET", "/wide", authenticated) is None


def test_parameter_and_dynamic_routes_keep_wide_capability_masks() -> None:
    required = 1 | (1 << 100)
    table = _core.PolicyRouteTable()
    parameter = object()
    greedy = object()
    hosted = object()
    table.add("/items/{item}", "GET", parameter, (required,))
    table.add_dynamic("/files/{rest:path}", "GET", None, greedy, (required,))
    table.add_dynamic(
        "/items/{item}", "GET", "{tenant}.example.test", hosted, (required,)
    )
    table.compile()

    assert table.match("GET", "/items/42", required) == (
        parameter,
        {"item": "42"},
    )
    classification, ticket = table.classify_request(
        "GET", "/items/42", "acme.example.test"
    )
    assert classification == 2
    assert table.resolve(ticket, required) == (
        hosted,
        {"item": "42", "tenant": "acme"},
    )
    classification, ticket = table.classify_request(
        "GET", "/files/reports/annual.pdf", "elsewhere.test"
    )
    assert classification == 2
    assert table.resolve(ticket, required) == (
        greedy,
        {"rest": "reports/annual.pdf"},
    )


def test_identity_resolution_builds_a_wide_mask_inside_native_code() -> None:
    authenticated = 1
    high = 1 << 100
    table = _core.PolicyRouteTable()
    handler = object()
    table.add("/wide/{item}", "GET", handler, (authenticated | high,))
    classification, ticket = table.classify("GET", "/wide/42")

    assert classification == 2
    descriptor = (authenticated, {"high": high}, {})
    assert table.resolve_identity(ticket, descriptor, {"high"}, set()) == (
        handler,
        {"item": "42"},
    )
    assert table.resolve_identity(ticket, descriptor, set(), set()) is None


@pytest.mark.parametrize("clause", [-1, "permission"])
def test_access_clauses_refuse_invalid_masks_at_registration(clause: object) -> None:
    table = _core.PolicyRouteTable()
    with pytest.raises(
        ValueError,
        match=r"access_clauses\[0\] must be a non-negative int",
    ):
        table.add("/invalid", "GET", object(), (clause,))


def test_application_compiles_more_than_64_distinct_capabilities() -> None:
    app = Wreath()
    names = [f"perm{index:03d}" for index in range(_WIDE)]

    for name in names:
        @app.get(f"/{name}", permissions=(name,))
        async def _handler(request):  # pragma: no cover - never invoked
            return {}

    app._compile_routes()
    assert len(app._capabilities) == _WIDE + 1

    from wreath._auth.models import Identity

    last = names[-1]
    mask = app._identity_mask(
        Identity(id="u1", roles=frozenset(), permissions=frozenset({last}))
    )
    assert mask & app._capabilities[f"permission:{last}"]
    # And a caller holding all the *other* permissions is still refused the last.
    others = app._identity_mask(
        Identity(id="u2", roles=frozenset(), permissions=frozenset(names[:-1]))
    )
    assert not others & app._capabilities[f"permission:{last}"]
