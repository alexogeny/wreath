"""The shared vocabulary of the two-tenant fixture: names, roles, and its DSN.

Everything here exists so the isolation tests can be *falsified*. They point a
hostile query at another tenant and require the server to refuse, and that claim
is only worth making against a database with two tenants, a central schema, and
an application role that is not the one that owns any of it.

The setup connection is the DSN's own role, which in the test container is a
superuser -- exactly the role `verify_isolation` refuses for the request path.
That is the point: the fixture uses it to *build* the deployment, and every
assertion runs on a second connection as an unprivileged `NOINHERIT` login role,
which is what a real application would use.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import pytest

from wreath.tenancy import Tenant, TenantStatus

DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(DSN is None, reason="WREATH_TEST_POSTGRES_DSN is unset")

#: The unprivileged role every assertion connects as.
APP_ROLE = "wreath_tenancy_app"
APP_PASSWORD = "tenancy-fixture"
CENTRAL = "wreath_tenancy_central"

ACME = Tenant(key="wt_acme", schema="tenant_wt_acme", role="tenant_wt_acme",
              status=TenantStatus.ACTIVE)
GLOBEX = Tenant(key="wt_globex", schema="tenant_wt_globex", role="tenant_wt_globex",
                status=TenantStatus.ACTIVE)


def app_dsn() -> str:
    """The fixture DSN with the application role's credentials substituted."""
    parts = urlsplit(str(DSN))
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((
        parts.scheme, f"{APP_ROLE}:{APP_PASSWORD}@{host}{port}", parts.path,
        parts.query, parts.fragment,
    ))
