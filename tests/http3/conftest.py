"""Shared fixtures for HTTP/3 tests.

Two layers:

* Availability/isolation tests (``test_availability.py``) always run and prove the
  default, no-QUIC build imports cleanly and that requesting ``h3`` fails with a
  clear error instead of silently downgrading.
* Behavioral tests require the optional ``wreath._native._http3`` backend and are
  skipped when it is not built. In the dedicated HTTP/3 CI job (backend present)
  they must run, not skip.
"""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import shutil
import subprocess
import tempfile

import pytest

from wreath.server import _http3_available


def http3_backend_available() -> bool:
    return _http3_available()


def curl_has_http3() -> bool:
    curl = shutil.which("curl")
    if curl is None:
        return False
    try:
        out = subprocess.run([curl, "--version"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "HTTP3" in out or "http3" in out


requires_h3 = pytest.mark.skipif(
    not http3_backend_available(),
    reason="optional wreath._native._http3 backend not built (WREATH_BUILD_HTTP3=1)",
)

requires_curl_h3 = pytest.mark.skipif(
    not (http3_backend_available() and curl_has_http3()),
    reason="needs the HTTP/3 backend and an HTTP/3-capable curl",
)


def make_self_signed_cert() -> tuple[str, str]:
    """Create a throwaway self-signed cert/key for HTTP/3 TLS."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover
        pytest.skip("cryptography not available")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = f"{d}/cert.pem", f"{d}/key.pem"
    with open(cp, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    return cp, kp


async def curl_http3(port: int, path: str, *extra: str, deadline: float = 12.0
                     ) -> tuple[int, bytes]:
    """Run an HTTP/3-only curl request against the loopback server."""
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "--http3-only", "-k", "--max-time", str(int(deadline)),
        *extra, f"https://127.0.0.1:{port}{path}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=deadline + 5)
    return proc.returncode, out


@pytest.fixture
def h3_module():
    if not http3_backend_available():
        pytest.skip("wreath._native._http3 not built")
    spec = importlib.util.find_spec("wreath._native._http3")
    assert spec is not None
    import wreath._native._http3 as mod  # noqa: PLC0415

    return mod
