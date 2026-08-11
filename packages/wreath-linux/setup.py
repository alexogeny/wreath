"""Build the extension installed by ``wreath[linux]``."""

from __future__ import annotations

import os
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

PACKAGE = Path(__file__).resolve().parent
NATIVE = Path("src/wreath/_native")


class _ParallelBuildExt(build_ext):
    def finalize_options(self) -> None:
        super().finalize_options()
        if self.parallel is None:
            self.parallel = os.cpu_count() or 1


sources = [str(NATIVE / name) for name in (
    "_reactormodule.c",
    "reactor_wheel.c",
    "reactor_tls.c",
)]

setup(
    packages=[],
    ext_modules=[
        Extension(
            "wreath._native._reactor",
            sources=sources,
            libraries=["ssl", "crypto"],
            extra_compile_args=["-O3", "-std=c11", "-fvisibility=hidden", "-flto"],
            extra_link_args=["-flto"],
        )
    ],
    cmdclass={"build_ext": _ParallelBuildExt},
)
