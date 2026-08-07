"""Build configuration for the optional wreath._native C accelerator.

The extension is optional at runtime: every native function has a pure-Python
twin in wreath._pure, selected automatically when the compiled module is absent
or WREATH_PURE=1 is set. Building requires only a C compiler and CPython headers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from setuptools import Extension, setup

profile_build = os.environ.get("WREATH_NATIVE_PROFILE") == "1"
if sys.platform == "win32":
    extra_compile_args = ["/O2"]
    hot_compile_args = [*extra_compile_args, "/GL"]
    hot_link_args = ["/LTCG"]
    if profile_build:
        extra_compile_args += ["/Zi", "/Oy-"]
        hot_compile_args += ["/Zi", "/Oy-"]
else:
    extra_compile_args = ["-O2", "-std=c11", "-fvisibility=hidden"]
    hot_compile_args = [*extra_compile_args, "-O3", "-flto"]
    hot_link_args = ["-flto"]
    if profile_build:
        extra_compile_args += ["-g", "-fno-omit-frame-pointer"]
        hot_compile_args += ["-g", "-fno-omit-frame-pointer"]

# Profile-guided optimization, in two passes and off by default.
#
#   WREATH_PGO=generate python setup.py build_ext --inplace
#   ... run a representative workload, which writes .gcda counters ...
#   WREATH_PGO=use      python setup.py build_ext --inplace
#
# Worth having because the hot C here is exactly the shape PGO helps: an HTTP
# parser and a dispatcher, dense in branches whose direction is stable in
# production and invisible to the compiler. `-O3 -flto` already lays out code
# well; what it cannot know is which way each branch actually goes.
#
# `-fprofile-dir` is absolute so both passes agree on where counters live --
# setuptools builds objects under a platform-specific `build/temp.*` path, and
# a relative profile directory silently produced "no profile data" on the second
# pass while still linking a valid, unoptimized extension.
#
# `-fprofile-partial-training` tells GCC to optimize un-exercised functions
# normally rather than for size; without it, any path the training run missed
# gets pessimized, which is the classic way a PGO build is faster on the
# benchmark and slower everywhere else.
pgo_mode = os.environ.get("WREATH_PGO", "")
if pgo_mode and sys.platform != "win32":
    profile_dir = os.path.abspath(os.environ.get("WREATH_PGO_DIR", "build/pgo"))
    os.makedirs(profile_dir, exist_ok=True)
    if pgo_mode == "generate":
        pgo_args = [f"-fprofile-dir={profile_dir}", "-fprofile-generate"]
        pgo_link = [f"-fprofile-dir={profile_dir}", "-fprofile-generate"]
    elif pgo_mode == "use":
        pgo_args = [
            f"-fprofile-dir={profile_dir}",
            "-fprofile-use",
            "-fprofile-correction",
            "-fprofile-partial-training",
            "-Wno-missing-profile",
        ]
        pgo_link = [f"-fprofile-dir={profile_dir}"]
    else:
        raise SystemExit(f"WREATH_PGO must be 'generate' or 'use', got {pgo_mode!r}")
    # Only the `hot_*` extensions -- `_reactor` and `_server`, the poller and
    # the HTTP parser/dispatcher. They are what PGO is for, and they are the
    # only ones declaring *both* compile and link arguments. Instrumenting an
    # extension whose Extension() passes no `extra_link_args` produces objects
    # referencing `__gcov_*` with no gcov runtime linked in: the build succeeds,
    # the `.so` fails to import, and `_core` comes back as None -- which reads
    # as `WREATH_PURE=1` and silently turns every benchmark into a no-op.
    hot_compile_args += pgo_args
    hot_link_args += pgo_link


def _http3_extension() -> Extension:
    """Configure the optional HTTP/3 extension (WREATH_BUILD_HTTP3=1).

    Detects the required QUIC libraries (ngtcp2, nghttp3, and a QUIC-capable TLS
    provider) via pkg-config and fails the *requested* HTTP/3 build with an
    actionable error when they are missing. This is only ever called when
    WREATH_BUILD_HTTP3=1; a default build never references these libraries and needs
    only a C compiler and CPython headers.
    """
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        raise SystemExit(
            "WREATH_BUILD_HTTP3=1 requires pkg-config to locate ngtcp2/nghttp3. "
            "Install pkg-config and the QUIC libraries, or unset WREATH_BUILD_HTTP3."
        )

    def have(name: str) -> bool:
        return subprocess.run([pkg_config, "--exists", name], check=False).returncode == 0

    # The ngtcp2 crypto backend name depends on the TLS provider: OpenSSL 3.5+
    # ships "ossl", the quictls fork ships "quictls". Prefer the OpenSSL backend.
    crypto = next(
        (c for c in ("libngtcp2_crypto_ossl", "libngtcp2_crypto_quictls") if have(c)),
        None,
    )
    base = ["libngtcp2", "libnghttp3"]
    missing = [name for name in base if not have(name)]
    if crypto is None:
        missing.append("libngtcp2_crypto_ossl|libngtcp2_crypto_quictls")
    if missing:
        raise SystemExit(
            "WREATH_BUILD_HTTP3=1 was requested but these QUIC libraries were not "
            f"found via pkg-config: {', '.join(missing)}. Install ngtcp2 (with an "
            "OpenSSL 3.5+ or quictls crypto backend) and nghttp3, or unset "
            "WREATH_BUILD_HTTP3 for a default build."
        )
    required = [*base, crypto]

    def pc(*args: str) -> list[str]:
        out = subprocess.run(
            [pkg_config, *args, *required],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return out.split()

    return Extension(
        "wreath._native._http3",
        sources=[
            "src/wreath/_native/_http3module.c",
            "src/wreath/_native/http3_connection.c",
            "src/wreath/_native/http3_asgi.c",
        ],
        depends=[
            "src/wreath/_native/http3.h",
            "src/wreath/_native/ascii.h",
        ],
        extra_compile_args=extra_compile_args + pc("--cflags"),
        extra_link_args=pc("--libs"),
    )


ext_modules = [
        Extension(
            "wreath._native._core",
            sources=[
                "src/wreath/_native/_coremodule.c",
                "src/wreath/_native/authz.c",
                "src/wreath/_native/cedar.c",
                "src/wreath/_native/env.c",
                "src/wreath/_native/headers.c",
                "src/wreath/_native/codecs.c",
                "src/wreath/_native/validate.c",
                "src/wreath/_native/orm_shape.c",
                "src/wreath/_native/ws.c",
                "src/wreath/_native/multipart.c",
                "src/wreath/_native/json.c",
                "src/wreath/_native/simd.c",
                "src/wreath/_native/msgpack.c",
                "src/wreath/_native/aesgcm.c",
                "src/wreath/_native/geospatial.c",
                "src/wreath/_native/protobuf.c",
                "src/wreath/_native/sse.c",
                "src/wreath/_native/xml.c",
                "src/wreath/_native/templates.c",
                "src/wreath/_native/http.c",
                "src/wreath/_native/router.c",
                "src/wreath/_native/dtrouter.c",
                "src/wreath/_native/dtbitset.c",
                "src/wreath/_native/security.c",
                "src/wreath/_native/hmac_sha256.c",
                "src/wreath/_native/jose.c",
                "src/wreath/_native/webpolicy.c",
                "src/wreath/_native/observability.c",
                "src/wreath/_native/proxy.c",
                "src/wreath/_native/ratelimit.c",
                "src/wreath/_native/kv.c",
                "src/wreath/_native/queue.c",
                "src/wreath/_native/scheduler.c",
            ],
            depends=[
                "src/wreath/_native/wreathcore.h",
                "src/wreath/_native/byteorder.h",
                "src/wreath/_native/ascii.h",
                "src/wreath/_native/hmac_sha256.h",
                "src/wreath/_native/bytes_writer.h",
                "src/wreath/_native/simd.h",
            ],
            extra_compile_args=extra_compile_args,
        ),
        Extension(
            "wreath._native._client",
            sources=[
                "src/wreath/_native/_clientmodule.c",
                "src/wreath/_native/client_http1.c",
                "src/wreath/_native/http.c",
            ],
            depends=[
                "src/wreath/_native/wreathcore.h",
                "src/wreath/_native/byteorder.h",
                "src/wreath/_native/ascii.h",
                "src/wreath/_native/hmac_sha256.h",
                "src/wreath/_native/bytes_writer.h",
            ],
            extra_compile_args=extra_compile_args,
        ),
        Extension(
            # wreath.edge's request path. Native-only by design (AGENTS.md), so
            # this is the implementation rather than an accelerator -- there is
            # no Python behind it to fall back to.
            "wreath._native._edge",
            sources=[
                "src/wreath/_native/_edgemodule.c",
                "src/wreath/_native/edge_headers.c",
                "src/wreath/_native/edge_serve.c",
            ],
            depends=[
                "src/wreath/_native/edge.h",
                "src/wreath/_native/wreath_stream.h",
                "src/wreath/_native/wreathcore.h",
                "src/wreath/_native/byteorder.h",
                "src/wreath/_native/ascii.h",
                "src/wreath/_native/hmac_sha256.h",
                "src/wreath/_native/bytes_writer.h",
            ],
            extra_compile_args=hot_compile_args,
            extra_link_args=hot_link_args,
        ),
        Extension(
            "wreath._native._server",
            sources=[
                "src/wreath/_native/_servermodule.c",
                "src/wreath/_native/server_common.c",
                "src/wreath/_native/http.c",
                "src/wreath/_native/server_request.c",
                "src/wreath/_native/server_http1.c",
                "src/wreath/_native/server_http2.c",
                "src/wreath/_native/server_hpack.c",
            ],
            depends=[
                "src/wreath/_native/server.h",
                "src/wreath/_native/wreath_stream.h",
                "src/wreath/_native/wreathcore.h",
                "src/wreath/_native/byteorder.h",
                "src/wreath/_native/ascii.h",
                "src/wreath/_native/hmac_sha256.h",
                "src/wreath/_native/bytes_writer.h",
                "src/wreath/_native/simd.h",
                "src/wreath/_native/server_request.c",
            ],
            extra_compile_args=hot_compile_args,
            extra_link_args=hot_link_args,
        ),
        Extension(
            "wreath._native._postgres",
            sources=[
                "src/wreath/_native/_postgresmodule.c",
                "src/wreath/_native/postgres/buffer.c",
                "src/wreath/_native/postgres/slab.c",
                "src/wreath/_native/postgres/codec.c",
                "src/wreath/_native/postgres/tape.c",
                "src/wreath/_native/postgres/decode.c",
                "src/wreath/_native/postgres/protocol.c",
                "src/wreath/_native/postgres/operation.c",
                "src/wreath/_native/postgres/record.c",
                "src/wreath/_native/postgres/model.c",
                "src/wreath/_native/postgres/hydrate.c",
                "src/wreath/_native/postgres/migration_artifact.c",
                "src/wreath/_native/postgres/migration_image.c",
                "src/wreath/_native/postgres/migration_resolver.c",
                "src/wreath/_native/postgres/migration_runner.c",
                "src/wreath/_native/postgres/migration_sql.c",
                "src/wreath/_native/postgres/plan.c",
                "src/wreath/_native/postgres/connection.c",
                "src/wreath/_native/postgres/pipeline.c",
            ],
            depends=[
                "src/wreath/_native/wreath_stream.h",
                "src/wreath/_native/byteorder.h",
                "src/wreath/_native/ascii.h",
                "src/wreath/_native/hmac_sha256.h",
                "src/wreath/_native/bytes_writer.h",
                "src/wreath/_native/postgres/buffer.h",
                "src/wreath/_native/postgres/slab.h",
                "src/wreath/_native/postgres/codec.h",
                "src/wreath/_native/postgres/tape.h",
                "src/wreath/_native/postgres/decode.h",
                "src/wreath/_native/postgres/pipeline.h",
                "src/wreath/_native/postgres/protocol.h",
                "src/wreath/_native/postgres/operation.h",
                "src/wreath/_native/postgres/record.h",
                "src/wreath/_native/postgres/model.h",
                "src/wreath/_native/postgres/hydrate.h",
                "src/wreath/_native/postgres/migration_artifact.h",
                "src/wreath/_native/postgres/migration_image.h",
                "src/wreath/_native/postgres/migration_resolver.h",
                "src/wreath/_native/postgres/migration_runner.h",
                "src/wreath/_native/postgres/migration_sql.h",
                "src/wreath/_native/postgres/plan.h",
                "src/wreath/_native/postgres/connection.h",
            ],
            extra_compile_args=extra_compile_args,
        ),
    ]

# HTTP/3 is explicit: a default build remains compiler-and-CPython-headers only,
# while a requested build fails loudly if its linked libraries are unavailable.
# --- platform-gated extensions ----------------------------------------------
# Everything above compiles anywhere: no platform headers, no POSIX-only calls.
# These two do not, and gating them is what lets macOS and Windows have the rest
# of the accelerators instead of failing the install at the first `#include`.
#
# The Python side was already built for their absence, which is why this is a
# packaging change and not a port: `wreath._native.__init__` resolves each
# through `try: import ... except ImportError: None`, `wreath.reactor` raises a
# named error only when `timers="wheel"` is explicitly asked for, and
# `wreath.server._create_recorder` returns None on a missing `_flight`, leaving
# every recorder hook a not-taken branch.

# io_uring, eventfd and raw `syscall()`: Linux and nowhere else. Without it the
# metal tier is unavailable and asyncio's own loop serves, which is the
# documented default anyway.
if sys.platform.startswith("linux"):
    ext_modules.append(
        Extension(
            "wreath._native._reactor",
            sources=[
                "src/wreath/_native/_reactormodule.c",
                "src/wreath/_native/reactor_wheel.c",
                "src/wreath/_native/reactor_tls.c",
            ],
            depends=[
                "src/wreath/_native/server.h",
                "src/wreath/_native/wreath_stream.h",
                "src/wreath/_native/reactor_internal.h",
                "src/wreath/_native/reactor_tls.h",
                "src/wreath/_native/reactor_ring.c",
                "src/wreath/_native/reactor_buffers.c",
                "src/wreath/_native/reactor_transport.c",
                "src/wreath/_native/reactor_poller.c",
            ],
            # OpenSSL, for TLS terminated in C. The metal tier is Linux-only and
            # already builds against system libraries; `_http3` links the same
            # libssl for its QUIC handshake.
            libraries=["ssl", "crypto"],
            extra_compile_args=hot_compile_args,
            extra_link_args=hot_link_args,
        )
    )

# `mmap` and `unistd.h`: every POSIX platform, so macOS builds it and Windows
# does not. A Windows build therefore has no Flight Recorder, and telemetry,
# `wreath.logging` and the Inspector are unavailable there rather than wrong --
# `Mode.OFF` is the default, so nothing else changes.
if sys.platform != "win32":
    ext_modules.append(
        Extension(
            "wreath._native._flight",
            sources=[
                "src/wreath/_native/_flightmodule.c",
                "src/wreath/_native/flight.c",
            ],
            depends=[
                "src/wreath/_native/flight.h",
                "src/wreath/_native/flight_schema.h",
            ],
            extra_compile_args=extra_compile_args,
        )
    )

if os.environ.get("WREATH_BUILD_HTTP3") == "1":
    ext_modules.append(_http3_extension())

# Experiments never participate in production backend selection. Building them
# is explicit so unfinished kernel requirements cannot affect normal installs.

setup(ext_modules=ext_modules)
