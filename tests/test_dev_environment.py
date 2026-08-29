from __future__ import annotations

import importlib.util
from pathlib import Path

import wreath


def test_cryptography_is_installed_for_tls_and_http3_tests() -> None:
    assert importlib.util.find_spec("cryptography") is not None, (
        "cryptography is missing: the TLS and HTTP/3 tests will skip themselves "
        "rather than fail. Run `uv sync --group dev`."
    )


def test_setuptools_is_installed_for_native_rebuilds() -> None:
    assert importlib.util.find_spec("setuptools") is not None, (
        "setuptools is missing: native rebuilds fail while the stale extension "
        "stays importable, so tests run against old C. Run `uv sync --group dev`."
    )


def test_wreath_declares_its_inline_types_to_consumers() -> None:
    package = Path(wreath.__file__).parent
    assert (package / "py.typed").is_file()
