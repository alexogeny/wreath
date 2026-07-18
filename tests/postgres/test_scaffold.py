from __future__ import annotations

import os
import subprocess
import sys

import wreath.postgres as postgres
from wreath._pure import postgres as pure_postgres


def test_postgres_facade_selects_an_available_backend() -> None:
    assert postgres._implementation in {"native", "pure"}


def test_reference_backend_identifies_itself() -> None:
    assert pure_postgres._implementation == "pure"


def test_wreath_pure_forces_reference_postgres_backend() -> None:
    env = os.environ.copy()
    env["WREATH_PURE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import wreath.postgres as postgres; print(postgres._implementation)",
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert completed.stdout.strip() == "pure"


def test_slice_one_connection_exports_remain_public() -> None:
    assert {
        "Connection",
        "InterfaceError",
        "OperationalError",
        "PipelineFullError",
        "PostgresError",
        "ProtocolError",
        "Record",
        "connect",
    } <= set(postgres.__all__)
