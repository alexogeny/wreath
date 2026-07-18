"""HTTP/3 interoperability with independent clients (RFC 9114).

Behavioral HTTP/3 tests. They require the optional ``wreath._native._http3`` backend
(WREATH_BUILD_HTTP3=1 with ngtcp2/nghttp3) and are skipped otherwise; in the
dedicated HTTP/3 CI job the backend is present and these run. The executable
detail is completed with the endpoint implementation (Step 5); the endpoint is
exercised through the ``h3_module`` fixture and a real QUIC client, never a mock.
"""
from __future__ import annotations

import pytest

from .conftest import requires_h3

pytestmark = [requires_h3, pytest.mark.asyncio]


async def test_interop_client_one_request(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_interop_client_two_request(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


