"""Isolated setuptools entry point for the sanitized native test executor."""

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
    name="wreath-sanitized-testrunner",
    version="0",
    ext_modules=[
        Extension(
            "wreath._native._testrunner",
            sources=[str(ROOT / "src/wreath/_native/_testrunnermodule.c")],
            extra_compile_args=FLAGS,
            extra_link_args=["-fsanitize=address,undefined"],
        )
    ],
)
