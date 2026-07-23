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
        depends=["src/wreath/_native/http3.h"],
        extra_compile_args=extra_compile_args + pc("--cflags"),
        extra_link_args=pc("--libs"),
    )


def _experimental_reactor_extension() -> Extension:
    """Build the isolated, non-production reactor research tier."""
    root = "src/wreath/_exp/reactor"
    domains = [
        "deadline_lanes",
        "connection_core",
        "buffer_ladder",
        "completion_batch",
        "send_chain",
        "fixed_files",
        "adaptive_policy",
        "uring_timeout",
    ]
    return Extension(
        "wreath._exp._reactor",
        sources=[
            "src/wreath/_exp/_reactormodule.c",
            *(f"{root}/{name}.c" for name in domains),
        ],
        depends=[
            "src/wreath/_exp/reactor/domain.h",
            *(f"{root}/{name}.h" for name in domains),
        ],
        extra_compile_args=extra_compile_args,
    )


ext_modules = [
        Extension(
            "wreath._native._core",
            sources=[
                "src/wreath/_native/_coremodule.c",
                "src/wreath/_native/authz.c",
                "src/wreath/_native/env.c",
                "src/wreath/_native/headers.c",
                "src/wreath/_native/codecs.c",
                "src/wreath/_native/validate.c",
                "src/wreath/_native/orm_shape.c",
                "src/wreath/_native/ws.c",
                "src/wreath/_native/multipart.c",
                "src/wreath/_native/json.c",
                "src/wreath/_native/templates.c",
                "src/wreath/_native/http.c",
                "src/wreath/_native/router.c",
                "src/wreath/_native/dtrouter.c",
                "src/wreath/_native/dtbitset.c",
                "src/wreath/_native/security.c",
                "src/wreath/_native/webpolicy.c",
                "src/wreath/_native/observability.c",
                "src/wreath/_native/proxy.c",
                "src/wreath/_native/ratelimit.c",
                "src/wreath/_native/scheduler.c",
            ],
            depends=["src/wreath/_native/wreathcore.h"],
            extra_compile_args=extra_compile_args,
        ),
        Extension(
            "wreath._native._client",
            sources=[
                "src/wreath/_native/_clientmodule.c",
                "src/wreath/_native/client_http1.c",
                "src/wreath/_native/http.c",
            ],
            depends=["src/wreath/_native/wreathcore.h"],
            extra_compile_args=extra_compile_args,
        ),
        Extension(
            "wreath._native._reactor",
            sources=[
                "src/wreath/_native/_reactormodule.c",
                "src/wreath/_native/reactor_wheel.c",
            ],
            depends=[
                "src/wreath/_native/server.h",
                "src/wreath/_native/wreath_stream.h",
                "src/wreath/_native/reactor_internal.h",
                "src/wreath/_native/reactor_ring.c",
                "src/wreath/_native/reactor_buffers.c",
                "src/wreath/_native/reactor_transport.c",
                "src/wreath/_native/reactor_poller.c",
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
                "src/wreath/_native/server_request.c",
            ],
            extra_compile_args=hot_compile_args,
            extra_link_args=hot_link_args,
        ),
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
                "src/wreath/_native/postgres/plan.c",
                "src/wreath/_native/postgres/connection.c",
                "src/wreath/_native/postgres/pool.c",
            ],
            depends=[
                "src/wreath/_native/wreath_stream.h",
                "src/wreath/_native/postgres/buffer.h",
                "src/wreath/_native/postgres/slab.h",
                "src/wreath/_native/postgres/codec.h",
                "src/wreath/_native/postgres/tape.h",
                "src/wreath/_native/postgres/decode.h",
                "src/wreath/_native/postgres/protocol.h",
                "src/wreath/_native/postgres/operation.h",
                "src/wreath/_native/postgres/record.h",
                "src/wreath/_native/postgres/model.h",
                "src/wreath/_native/postgres/hydrate.h",
                "src/wreath/_native/postgres/plan.h",
                "src/wreath/_native/postgres/connection.h",
                "src/wreath/_native/postgres/pool.h",
            ],
            extra_compile_args=extra_compile_args,
        ),
    ]

# HTTP/3 is explicit: a default build remains compiler-and-CPython-headers only,
# while a requested build fails loudly if its linked libraries are unavailable.
if os.environ.get("WREATH_BUILD_HTTP3") == "1":
    ext_modules.append(_http3_extension())

# Experiments never participate in production backend selection. Building them
# is explicit so unfinished kernel requirements cannot affect normal installs.
if os.environ.get("WREATH_BUILD_EXP") == "1":
    ext_modules.append(_experimental_reactor_extension())

setup(ext_modules=ext_modules)
