"""Isolated setuptools entry point for the sanitized PostgreSQL extension."""

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
SOURCES = [
    "_postgresmodule.c",
    "postgres/buffer.c",
    "postgres/slab.c",
    "postgres/codec.c",
    "postgres/tape.c",
    "postgres/decode.c",
    "postgres/protocol.c",
    "postgres/operation.c",
    "postgres/record.c",
    "postgres/model.c",
    "postgres/hydrate.c",
    "postgres/plan.c",
    "postgres/connection.c",
    "postgres/pool.c",
]

setup(
    name="wreath-sanitized-postgres",
    version="0",
    ext_modules=[
        Extension(
            "wreath._native._postgres",
            sources=[str(ROOT / "src/wreath/_native" / source) for source in SOURCES],
            extra_compile_args=FLAGS,
            extra_link_args=["-fsanitize=address,undefined"],
        )
    ],
)
