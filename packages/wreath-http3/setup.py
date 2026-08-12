"""Build the extension installed by ``wreath[h3]`` or ``wreath[http3]``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

if sysconfig.get_config_var("Py_GIL_DISABLED"):
    raise RuntimeError(
        "wreath-http3 supports regular CPython 3.14; free-threaded CPython "
        "3.14t is not supported. Use a regular CPython 3.14 interpreter."
    )

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

PACKAGE = Path(__file__).resolve().parent
NATIVE = Path("src/wreath/_native")


class _ParallelBuildExt(build_ext):
    def finalize_options(self) -> None:
        super().finalize_options()
        if self.parallel is None:
            self.parallel = os.cpu_count() or 1


def _quic_flags() -> tuple[list[str], list[str]]:
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        raise SystemExit("wreath-http3 requires pkg-config")

    def have(name: str) -> bool:
        return subprocess.run([pkg_config, "--exists", name], check=False).returncode == 0

    crypto = next(
        (name for name in ("libngtcp2_crypto_ossl", "libngtcp2_crypto_quictls")
         if have(name)),
        None,
    )
    required = ["libngtcp2", "libnghttp3"]
    missing = [name for name in required if not have(name)]
    if crypto is None:
        missing.append("libngtcp2_crypto_ossl|libngtcp2_crypto_quictls")
    if missing:
        raise SystemExit(
            "wreath-http3 needs these QUIC libraries via pkg-config: "
            + ", ".join(missing)
        )
    required.append(crypto)

    def flags(option: str) -> list[str]:
        return subprocess.run(
            [pkg_config, option, *required],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()

    return flags("--cflags"), flags("--libs")


cflags, libs = _quic_flags()
sources = [str(NATIVE / name) for name in (
    "_http3module.c",
    "http3_connection.c",
    "http3_asgi.c",
    "server_policy.c",
    "http3_header_block.c",
)]

setup(
    packages=[],
    ext_modules=[
        Extension(
            "wreath._native._http3",
            sources=sources,
            depends=[
                str(NATIVE / "header_block.c"),
                str(NATIVE / "header_block.h"),
                str(NATIVE / "server_request_capi.h"),
                str(NATIVE / "server_policy.h"),
            ],
            extra_compile_args=["-O2", "-std=c11", "-fvisibility=hidden", *cflags],
            extra_link_args=libs,
        )
    ],
    cmdclass={"build_ext": _ParallelBuildExt},
)
