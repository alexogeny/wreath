from __future__ import annotations

import wreath.postgres as postgres
from wreath import _pgdriver as pure_postgres


def test_postgres_facade_selects_an_available_backend() -> None:
    assert postgres._implementation in {"native", "python"}


def test_reference_backend_identifies_itself() -> None:
    assert pure_postgres._implementation == "python"


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
