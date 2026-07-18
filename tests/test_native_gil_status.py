from __future__ import annotations

from wreath._devtools.native_gil_status import Probe, evaluate_probe


def test_free_threaded_import_that_enables_gil_is_unsafe() -> None:
    probe = Probe(
        module="wreath._native",
        free_threaded=True,
        status_available=True,
        gil_before=False,
        gil_after=True,
        imported=True,
        error=None,
    )

    assert evaluate_probe(probe) == "gil-enabled-by-import"


def test_free_threaded_import_that_leaves_gil_disabled_is_ready() -> None:
    probe = Probe(
        module="wreath._native",
        free_threaded=True,
        status_available=True,
        gil_before=False,
        gil_after=False,
        imported=True,
        error=None,
    )

    assert evaluate_probe(probe) == "gil-remained-disabled"


def test_standard_build_is_not_misreported_as_free_threaded() -> None:
    probe = Probe(
        module="wreath._native",
        free_threaded=False,
        status_available=True,
        gil_before=True,
        gil_after=True,
        imported=True,
        error=None,
    )

    assert evaluate_probe(probe) == "standard-build"


def test_import_error_is_reported_before_gil_classification() -> None:
    probe = Probe(
        module="wreath._native._http3",
        free_threaded=True,
        status_available=True,
        gil_before=False,
        gil_after=False,
        imported=False,
        error="module unavailable",
    )

    assert evaluate_probe(probe) == "import-error"
