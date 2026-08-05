"""Isolated setuptools entry point for the sanitized native reactor extension."""

from __future__ import annotations

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

setup(
    name="wreath-sanitized-reactor",
    version="0",
    ext_modules=[
        Extension(
            "wreath._native._reactor",
            sources=[
                str(ROOT / "src/wreath/_native" / name)
                for name in ("_reactormodule.c", "reactor_wheel.c",
                             "reactor_tls.c")
            ],
            # OpenSSL, for the native TLS transport. The session is C-owned and
            # freed on transport teardown, which is exactly the lifetime ASan
            # exists to check here.
            libraries=["ssl", "crypto"],
            extra_compile_args=FLAGS,
            extra_link_args=["-fsanitize=address,undefined"],
        )
    ],
)
