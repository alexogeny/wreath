"""HTTP/3 build isolation and availability (HTTP/3 is opt-in at build time).

These run in the default (no-QUIC) build and prove that:
  * Wreath imports cleanly without any HTTP/3 / QUIC libraries;
  * requesting ``h3`` fails with a clear error and never silently downgrades.
In the dedicated HTTP/3 CI job the backend is present and ``_http3_available()``
returns True; these tests remain valid there too.
"""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import tempfile

import pytest

import wreath  # noqa: F401  - importing the framework must not require QUIC libs
from wreath.server import ServerConfig, TLSConfig, _http3_available, serve


def _self_signed() -> tuple[str, str]:
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
            .sign(key, hashes.SHA256()))
    tmp = tempfile.mkdtemp()
    cert_path, key_path = f"{tmp}/cert.pem", f"{tmp}/key.pem"
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    return cert_path, key_path


def test_framework_imports_without_quic_libraries() -> None:
    # Importing wreath and wreath.server must not require the HTTP/3 extension.
    import wreath.server

    assert hasattr(wreath.server, "serve")


def _http3_loadable() -> bool:
    """Whether the extension actually imports, not merely whether a file exists.

    ``find_spec`` finds a loader without running module init, so it still says
    "present" for a partial build whose ``.so`` is there but whose transitive
    QUIC libraries (``libngtcp2_crypto_ossl`` and friends) are not. That state is
    real -- a machine that built the extension and later lost or upgraded the
    system libraries lands in it -- and ``_http3_available`` documents that it
    must report ``False`` there, so that ``serve()`` raises its actionable "not
    built" error instead of a raw ImportError from deep in the import machinery.
    """
    if importlib.util.find_spec("wreath._native._http3") is None:
        return False
    try:
        importlib.import_module("wreath._native._http3")
    except ImportError:
        return False
    return True


def test_http3_available_matches_extension_usability() -> None:
    # Loadability, not discoverability: that is the contract _http3_available
    # states, and comparing against find_spec instead made a partial build look
    # like a defect in wreath rather than an incomplete install.
    assert _http3_available() is _http3_loadable()


def test_native_server_import_does_not_pull_in_http3() -> None:
    # The HTTP/1.1+HTTP/2 extension must import independently of QUIC libraries.
    server_ext = importlib.import_module("wreath._native._server")
    assert hasattr(server_ext, "Http1Protocol")


@pytest.mark.asyncio
async def test_requesting_unbuilt_h3_raises_without_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wreath.server._http3_available", lambda: False)
    cert, key = _self_signed()
    tls = TLSConfig(certfile=cert, keyfile=key)
    with pytest.raises(RuntimeError, match="HTTP/3"):
        await serve(
            _noop_app,
            ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
            tls=tls,
        )


@pytest.mark.asyncio
async def test_h3_requires_tls_config() -> None:
    with pytest.raises(ValueError, match="TLSConfig"):
        await serve(
            _noop_app,
            ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
        )


def test_h3_in_protocol_config_is_valid() -> None:
    # The config itself accepts h3; only serving without a backend fails.
    cfg = ServerConfig(protocols=("h3",))
    assert cfg.protocols == ("h3",)
    cfg2 = ServerConfig(protocols=("http/1.1", "h2", "h3"))
    assert cfg2.protocols == ("http/1.1", "h2", "h3")


async def _noop_app(scope: dict, receive, send) -> None:  # pragma: no cover
    await asyncio.sleep(0)
