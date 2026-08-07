"""Pricing connection-per-tenant isolation, from measured numbers.

The module docstring used to end "which costs a connection pool per tenant",
which was a guess dressed as a verdict. Measured against PostgreSQL 17: 162 KiB
of *client* memory and ~15 MiB of *server* memory per connection, 4.19 ms to
open one. The cost is almost entirely on the database server, which is the
opposite of what the guess implied -- so it is a configuration with arithmetic
rather than a door marked expensive.
"""

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
    """The measurement that changed the recommendation.

    Guessing put the cost in the application, where it is negligible. It is in
    the database server by roughly two orders of magnitude, and that is what
    decides whether a fleet fits.
    """
    assert BACKEND_MEMORY_BYTES > 50 * CLIENT_MEMORY_BYTES


def test_a_small_fleet_fits_comfortably() -> None:
    """Tens of tenants is affordable, which is the case worth saying yes to."""
    budget = connection_budget(tenants=20, workers=4, max_connections=200)
    assert budget.required == 80
    assert budget.fits


def test_the_arithmetic_multiplies_by_workers() -> None:
    """**The half people forget.**

    One connection per tenant is one connection per tenant *per worker*, so a
    four-worker deployment needs four times what a single-process one measured.
    """
    assert connection_budget(tenants=50, workers=1).required == 50
    assert connection_budget(tenants=50, workers=4).required == 200


def test_a_large_fleet_does_not_fit_and_the_refusal_shows_the_working() -> None:
    """Naming the numbers rather than the conclusion.

    An operator who disagrees can raise `max_connections`, cut workers, or stay
    on role isolation -- and none of those choices is wreath's to make.
    """
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
    """PostgreSQL reserves connections for superusers and its own background
    work, and a fleet that exactly fills the ceiling leaves nothing for the
    operator who needs to get in and find out why."""
    budget = connection_budget(tenants=100, workers=1, max_connections=100)
    assert budget.required == 100
    assert not budget.fits


def test_the_default_ceiling_is_postgresqls_own_default() -> None:
    """Not something generous: a deployment that has never raised
    `max_connections` is the one this is most likely to surprise."""
    assert connection_budget(tenants=1).max_connections == 100


def test_the_estimate_is_reported_as_an_estimate() -> None:
    """It moves with `work_mem` and `shared_buffers`, so a figure presented as
    exact would be believed and then wrong on somebody else's server."""
    explanation = connection_budget(tenants=10, workers=1).explain()
    assert "estimate" in explanation
    assert "work_mem" in explanation


def test_a_budget_that_fits_raises_nothing() -> None:
    """So the refusal above is not passing for free."""
    require_connection_budget(connection_budget(tenants=10, workers=2))
