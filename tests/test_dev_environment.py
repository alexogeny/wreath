"""The dev environment must actually contain what the suite silently relies on.

Both of these used to be present only by accident — `cryptography` as a
transitive of the benchmark group, `setuptools` from whatever last pulled it in
— and neither was in the lockfile. `uv sync` reconciles the venv to exactly the
selected groups, so any sync removed them. The failure was invisible: the TLS
and HTTP/3 tests skip themselves when `cryptography` is missing, so the suite
stayed green while 21 of them stopped running, and native rebuilds failed with
ModuleNotFoundError while leaving a stale .so in place for the tests to pass
against.

These assert rather than skip. If you are running the test suite you have the
dev group, so a missing entry here is a broken environment, not a valid
configuration to tolerate.
"""

from __future__ import annotations

import importlib.util


def test_cryptography_is_installed_for_tls_and_http3_tests() -> None:
    """tests/http3/conftest.py and test_server_protocols.py mint certs with it."""
    assert importlib.util.find_spec("cryptography") is not None, (
        "cryptography is missing: the TLS and HTTP/3 tests will skip themselves "
        "rather than fail. Run `uv sync --group dev`."
    )


def test_setuptools_is_installed_for_native_rebuilds() -> None:
    """`python setup.py build_ext --inplace` and tools/sanitizers/ import it."""
    assert importlib.util.find_spec("setuptools") is not None, (
        "setuptools is missing: native rebuilds fail while the stale extension "
        "stays importable, so tests run against old C. Run `uv sync --group dev`."
    )
