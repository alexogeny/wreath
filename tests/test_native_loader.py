"""`wreath._native` resolves compiled extensions, and is the only thing that does.

Five modules used to call `importlib` themselves because this knew four of the
eight names. It knows all eight now, and these pin that: every name `setup.py`
builds resolves to the module when the build has it and to `None` when it has
not, and a name that is not one of the eight raises rather than reading as "this
build has not got it".
"""

from __future__ import annotations

import importlib

import pytest

from wreath import _native

#: Every extension `setup.py` builds.
ALL_EXTENSIONS = (
    "_core",
    "_client",
    "_postgres",
    "_server",
    "_reactor",
    "_edge",
    "_flight",
    "_http3",
)


def _built(name: str) -> bool:
    """Whether this build actually compiled `name`."""
    try:
        importlib.import_module(f"wreath._native.{name}")
    except ImportError:
        return False
    return True


@pytest.mark.parametrize("name", ALL_EXTENSIONS)
def test_every_built_extension_is_reachable_as_an_attribute(name: str) -> None:
    """`from wreath._native import _flight` must not be an AttributeError."""
    assert getattr(_native, name, "MISSING") != "MISSING", (
        f"{name} is built by setup.py but wreath._native does not declare it"
    )


@pytest.mark.parametrize("name", ALL_EXTENSIONS)
def test_a_built_extension_resolves_and_an_unbuilt_one_is_none(name: str) -> None:
    resolved = _native.extension(name)
    if _built(name):
        assert resolved is not None, f"{name} is compiled but extension() yielded None"
    else:
        assert resolved is None, f"{name} is not compiled but extension() yielded a module"


def test_an_unknown_extension_is_an_error_not_a_none() -> None:
    """A typo'd name must not read as "this build has not got it"."""
    with pytest.raises(AttributeError):
        _native.extension("_no_such_extension")
