"""Shared fixtures for HTTP/3 tests.

Two layers:

* Availability/isolation tests (``test_availability.py``) always run and prove the
  default, no-QUIC build imports cleanly and that requesting ``h3`` fails with a
  clear error instead of silently downgrading.
* Behavioral tests require the optional ``wreath._native._http3`` backend and are
  skipped when it is not built. In the dedicated HTTP/3 CI job (backend present)
  they must run, not skip.

The skip reasons diagnose rather than guess. "Not built" and "built but will not
load" are different failures with different fixes, and reporting the first for
the second costs real time: the extension can sit compiled on disk while a
transitive QUIC library is absent, at which point the honest answer is the
loader's error, not an instruction to build something that already exists.
``docs/guides/server.md`` carries the toolchain the build needs.
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


def _backend_skip_reason() -> str:
    """Explain why the HTTP/3 backend is unusable, in the loader's own words.

    Three outcomes, because they need three different fixes:

    * the extension was never compiled -- build it;
    * it compiled but a transitive QUIC library is missing from the loader path
      -- supply the library, do not rebuild;
    * it loads fine, in which case this string is never shown.

    The middle case is the one worth naming. ``ldd`` on the extension reports the
    unresolved library, but a skip reading "not built" points away from that and
    at a build that has in fact already happened.
    """
    if importlib.util.find_spec("wreath._native._http3") is None:
        return (
            "the optional HTTP/3 extension is not compiled; rebuild with "
            "WREATH_BUILD_HTTP3=1 (see docs/guides/server.md for the ngtcp2 / "
            "nghttp3 / OpenSSL 3.5+ toolchain it needs)"
        )
    try:
        importlib.import_module("wreath._native._http3")
    except (ImportError, ValueError) as exc:
        return (
            f"the HTTP/3 extension is compiled but will not load: {exc}. "
            "The build is present and a transitive QUIC library is not -- supply "
            "it on LD_LIBRARY_PATH rather than rebuilding (docs/guides/server.md)"
        )
    return "the HTTP/3 backend is available"


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
    reason=_backend_skip_reason(),
)

# Named separately from `requires_h3` so a skip says which of the two
# prerequisites is absent. Reporting "needs the backend and an HTTP/3-capable
# curl" when only one is missing sends the reader to check both.
requires_curl_h3 = pytest.mark.skipif(
    not (http3_backend_available() and curl_has_http3()),
    reason=(
        _backend_skip_reason()
        if not http3_backend_available()
        else "curl is absent or was built without HTTP/3 (check `curl --version` for HTTP3)"
    ),
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
    import wreath._native._http3 as mod

    return mod
