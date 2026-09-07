import pytest

from wreath._devtools import complexity_probe as complexity


@pytest.mark.parametrize(
    ("subject", "control", "degree"),
    [
        ("application-image-warm-lookups", "application-image-route-sweep-control", 1),
        ("kv-live-count", "kv-live-peek-control", 0),
        ("kv-empty-clear", "kv-empty-peek-control", 0),
    ],
)
def test_resource_contracts_have_same_size_controls(subject, control, degree):
    measured = complexity._REGISTRY[subject]
    reference = complexity._REGISTRY[control]
    assert measured.sizes == reference.sizes
    assert measured.expect == reference.expect == degree
    assert measured.todo is reference.todo is None
    assert measured.noise_floor > 0


@pytest.mark.parametrize("method", ["operation_id", "contract_candidates"])
def test_route_lookup_probe_refuses_wrong_output(monkeypatch, method):
    from wreath.app import _ApplicationImage

    monkeypatch.setattr(_ApplicationImage, method, lambda *args: "wrong")
    with pytest.raises(RuntimeError, match="declared route oracle"):
        complexity._application_image_lookup_harness(4, control=False)


@pytest.mark.parametrize(
    ("clear", "control"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_kv_probe_refuses_wrong_output(monkeypatch, clear, control):
    from types import SimpleNamespace

    from wreath._native import _core

    def wrong_table(**options):
        return SimpleNamespace(
            set=lambda *args, **kwargs: None,
            count=lambda **kwargs: options["max_entries"] + 1,
            clear=lambda: 1,
            peek=lambda *args: 99,
        )

    monkeypatch.setattr(_core, "KV", wrong_table)
    with pytest.raises(RuntimeError, match="live-entry oracle"):
        complexity._kv_lifecycle_harness(4, clear=clear, control=control)
