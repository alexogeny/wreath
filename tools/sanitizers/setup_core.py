"""Isolated setuptools entry point for the sanitized native _core extension."""

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

# Kept in step with the `wreath._native._core` extension in setup.py.
SOURCES = (
    "_coremodule.c",
    "activate.c",
    "authz.c",
    "cedar.c",
    "env.c",
    "headers.c",
    "codecs.c",
    "sql.c",
    "dkim.c",
    "scim.c",
    "curves.c",
    "validate.c",
    "orm_shape.c",
    "orm_relationship.c",
    "ws.c",
    "multipart.c",
    "json.c",
    "simd.c",
    "msgpack.c",
    "aesgcm.c",
    "geospatial.c",
    "protobuf.c",
    "sse.c",
    "xml.c",
    "templates.c",
    "response.c",
    "core_http.c",
    "core_header_block.c",
    "router.c",
    "dtrouter.c",
    "dtbitset.c",
    "security.c",
    "hmac_sha256.c",
    "jose.c",
    "grpc.c",
    "graphql.c",
    "graphql_parser.c",
    "flight_project.c",
    "recording.c",
    "signatures.c",
    "webpolicy.c",
    "observability.c",
    "series.c",
    "proxy.c",
    "ratelimit.c",
    "kv.c",
    "queue.c",
    "scheduler.c",
)

setup(
    name="wreath-sanitized-core",
    version="0",
    ext_modules=[
        Extension(
            "wreath._native._core",
            sources=[str(ROOT / "src/wreath/_native" / name) for name in SOURCES],
            extra_compile_args=FLAGS,
            extra_link_args=["-fsanitize=address,undefined"],
        )
    ],
)
