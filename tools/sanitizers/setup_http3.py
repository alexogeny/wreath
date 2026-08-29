"""Isolated setuptools entry point for the sanitized HTTP/3 extension.

The HTTP/3 response path hands nghttp3 raw pointers into Python bytes objects
and keeps them exposed until the peer acknowledges them, so ASan is the only
thing that can prove a retransmission never reads released storage.

Requires the optional QUIC libraries, exactly like the WREATH_BUILD_HTTP3=1 build.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import Extension, setup

ROOT = Path(__file__).resolve().parents[2]
FLAGS = [
    "-fsanitize=address,undefined",
    "-fno-omit-frame-pointer",
    "-fno-sanitize-recover=all",
    "-g",
    "-O1",
    "-std=c11",
]


def _quic_flags() -> tuple[list[str], list[str]]:
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        raise SystemExit("the sanitized HTTP/3 build requires pkg-config")

    def have(name: str) -> bool:
        return subprocess.run([pkg_config, "--exists", name], check=False).returncode == 0

    crypto = next(
        (c for c in ("libngtcp2_crypto_ossl", "libngtcp2_crypto_quictls") if have(c)),
        None,
    )
    required = ["libngtcp2", "libnghttp3"]
    missing = [name for name in required if not have(name)]
    if crypto is None:
        missing.append("libngtcp2_crypto_ossl|libngtcp2_crypto_quictls")
    if missing:
        raise SystemExit(
            f"the sanitized HTTP/3 build needs these QUIC libraries: {', '.join(missing)}"
        )
    required.append(crypto)

    def pc(*args: str) -> list[str]:
        out = subprocess.run(
            [pkg_config, *args, *required], check=True, capture_output=True, text=True
        ).stdout
        return out.split()

    return pc("--cflags"), pc("--libs")


cflags, libs = _quic_flags()

setup(
    name="wreath-sanitized-http3",
    version="0",
    script_args=sys.argv[1:],
    ext_modules=[
        Extension(
            "wreath._native._http3",
            sources=[
                str(ROOT / "src/wreath/_native/_http3module.c"),
                str(ROOT / "src/wreath/_native/http3_asgi.c"),
                str(ROOT / "src/wreath/_native/http3_connection.c"),
                str(ROOT / "src/wreath/_native/server_policy.c"),
                str(ROOT / "src/wreath/_native/http3_header_block.c"),
            ],
            extra_compile_args=[*FLAGS, *cflags],
            extra_link_args=["-fsanitize=address,undefined", *libs],
        )
    ],
)
