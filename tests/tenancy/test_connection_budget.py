from __future__ import annotations

import pytest

from wreath.tenancy import (
    BACKEND_MEMORY_BYTES,
    CLIENT_MEMORY_BYTES,
    TenancyError,
    connection_budget,
    require_connection_budget,
)


def test_the_server_side_cost_dominates_the_client_side() -> None:
    assert BACKEND_MEMORY_BYTES > 50 * CLIENT_MEMORY_BYTES


def test_a_small_fleet_fits_comfortably() -> None:
    budget = connection_budget(tenants=20, workers=4, max_connections=200)
    assert budget.required == 80
    assert budget.fits


def test_the_arithmetic_multiplies_by_workers() -> None:
    assert connection_budget(tenants=50, workers=1).required == 50
    assert connection_budget(tenants=50, workers=4).required == 200


def test_a_large_fleet_does_not_fit_and_the_refusal_shows_the_working() -> None:
    budget = connection_budget(tenants=200, workers=4, max_connections=200)
    assert not budget.fits
    with pytest.raises(TenancyError, match="does not fit") as raised:
        require_connection_budget(budget)
    message = str(raised.value)
    assert "800 backends" in message
    assert "max_connections=200" in message
    assert "GiB" in message
    assert "isolation='role'" in message


def test_a_fleet_that_exactly_fills_max_connections_is_refused() -> None:
    budget = connection_budget(tenants=100, workers=1, max_connections=100)
    assert budget.required == 100
    assert not budget.fits


def test_the_default_ceiling_is_postgresqls_own_default() -> None:
    assert connection_budget(tenants=1).max_connections == 100


def test_the_estimate_is_reported_as_an_estimate() -> None:
    explanation = connection_budget(tenants=10, workers=1).explain()
    assert "estimate" in explanation
    assert "work_mem" in explanation


def test_a_budget_that_fits_raises_nothing() -> None:
    require_connection_budget(connection_budget(tenants=10, workers=2))
