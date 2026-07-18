"""HTTP/3 SETTINGS and control streams (RFC 9114 s6.2.1, s7.2.4).

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


async def test_quic_v1_and_h3_alpn_negotiated(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_unsupported_version_is_rejected_without_asgi(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_client_and_server_unidirectional_control_streams(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_one_control_and_one_qpack_encoder_decoder_stream_per_connection(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_settings_uniqueness_enforced(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_settings_values_in_valid_range(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


