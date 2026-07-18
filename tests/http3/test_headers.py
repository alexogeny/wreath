"""HTTP/3 request header and QPACK validation (RFC 9114 s4, RFC 9204).

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


async def test_request_pseudo_headers_validated_like_h2(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_ordinary_header_validation_matches_h2_semantics(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_qpack_static_reference(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_qpack_dynamic_reference_and_unblocking(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_qpack_blocked_streams_bound_enforced(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_qpack_cancellation(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


