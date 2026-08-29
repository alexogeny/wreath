from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import pytest

from wreath.tenancy import Tenant, TenantStatus

DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(DSN is None, reason="WREATH_TEST_POSTGRES_DSN is unset")

#: Every name below is per-worker. A PostgreSQL **role is cluster-global** -- it
#: is not scoped by database or schema the way a table is -- so eight xdist
#: workers building this deployment from one set of constants were all dropping
#: and creating the same role at once. The loser of that race saw `duplicate key
#: value violates unique constraint "pg_authid_rolname_index"` out of `CREATE
#: ROLE`, or a missing schema underneath it, and the eleven errors it produced
#: looked like a tenancy defect rather than a fixture collision.
#:
#: Measured: this file is 22 passed at `--workers 1` and 1 failed / 11 errors at
#: `--workers 8` without the suffix.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")

#: The unprivileged role every assertion connects as.
APP_ROLE = f"wreath_tenancy_app_{_WORKER}"
APP_PASSWORD = "tenancy-fixture"
CENTRAL = f"wreath_tenancy_central_{_WORKER}"

ACME = Tenant(
    key="wt_acme",
    schema=f"tenant_wt_acme_{_WORKER}",
    role=f"tenant_wt_acme_{_WORKER}",
    status=TenantStatus.ACTIVE,
)
GLOBEX = Tenant(
    key="wt_globex",
    schema=f"tenant_wt_globex_{_WORKER}",
    role=f"tenant_wt_globex_{_WORKER}",
    status=TenantStatus.ACTIVE,
)


def app_dsn() -> str:
    """The fixture DSN with the application role's credentials substituted."""
    parts = urlsplit(str(DSN))
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (
            parts.scheme,
            f"{APP_ROLE}:{APP_PASSWORD}@{host}{port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )
