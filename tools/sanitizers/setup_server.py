"""Isolated setuptools entry point for the sanitized native server extension."""

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
    name="wreath-sanitized-server",
    version="0",
    ext_modules=[
        Extension(
            "wreath._native._server",
            sources=[
                str(ROOT / "src/wreath/_native/_servermodule.c"),
                str(ROOT / "src/wreath/_native/server_common.c"),
                str(ROOT / "src/wreath/_native/http.c"),
                str(ROOT / "src/wreath/_native/header_block.c"),
                str(ROOT / "src/wreath/_native/server_request.c"),
                str(ROOT / "src/wreath/_native/server_http1.c"),
                str(ROOT / "src/wreath/_native/server_http2.c"),
                str(ROOT / "src/wreath/_native/server_hpack.c"),
                str(ROOT / "src/wreath/_native/server_policy.c"),
            ],
            extra_compile_args=FLAGS,
            extra_link_args=["-fsanitize=address,undefined"],
        )
    ],
)
