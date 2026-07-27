"""Capability masks past 64 bits (report 23: R-44).

`Wreath._compile_capabilities` assigns `1 << index` per distinct role and
permission name, so an application with more than 64 of them produces clauses
wider than a machine word. This pins that the whole path -- mask building, the
pure table, and the native table -- stays exact there.
"""

from __future__ import annotations

import pytest

from wreath._pure.authz import build_capability_mask as pure_build_capability_mask
from wreath._pure.dtrouter import DecisionRouteTable as PureDecisionRouteTable
from wreath.app import Wreath

try:
    from wreath._native import _core
except ImportError:  # pragma: no cover
    _core = None

_TABLES = [
    pytest.param(PureDecisionRouteTable, id="pure"),
    pytest.param(
        None if _core is None else _core.DecisionRouteTable,
        id="native",
        marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
    ),
]

_BUILDERS = [
    pytest.param(pure_build_capability_mask, id="pure"),
    pytest.param(
        None if _core is None else _core.build_capability_mask,
        id="native",
        marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
    ),
]

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
