"""`wreath._native` is the one place that decides which implementation runs.

Before this, four of the eight compiled extensions were loaded by hand at eleven
sites across five modules, each re-deriving some subset of three questions --
does `WREATH_PURE=1` suppress this one, what happens when the build has not got
it, and does the answer get re-read per call. No two agreed, and one of the
disagreements was invisible: `wreath.replay` resolved the *native* HTTP/1 driver
under `WREATH_PURE=1` while `wreath.server` resolved the pure one, in the exact
mode whose whole job is proving the twins agree.

That divergence is deliberate -- replay is native-first because the pure driver
is expensive enough to drag a replayed run down to the performance of the
frameworks Wreath exists not to be -- so the tests below pin it *as a contract*
rather than removing it. What is not acceptable is it being an accident of which
loader someone copied.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from wreath import _native

#: Every extension `setup.py` builds. `wreath._native` must know all of them;
#: the ad-hoc loaders existed because it knew four.
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

#: The ones with a pure-Python twin to fall back to, so `WREATH_PURE=1` means
#: something for them. The rest are native by definition and load regardless.
HONOURS_PURE = frozenset({"_core", "_client", "_postgres", "_server"})


def _built(name: str) -> bool:
    """Whether this build actually compiled `name`, ignoring every gate."""
    try:
        importlib.import_module(f"wreath._native.{name}")
    except ImportError:
        return False
    return True


@pytest.mark.parametrize("name", ALL_EXTENSIONS)
def test_every_built_extension_is_reachable_as_an_attribute(name: str) -> None:
    """`from wreath._native import _flight` must not be an AttributeError.

    Four of these raised, which is why five modules called `importlib` instead.
    """
    assert getattr(_native, name, "MISSING") != "MISSING", (
        f"{name} is built by setup.py but wreath._native does not declare it"
    )


@pytest.mark.parametrize("name", ALL_EXTENSIONS)
def test_extension_declares_one_pure_policy_per_name(name: str, monkeypatch: Any) -> None:
    """`WREATH_PURE=1` suppresses exactly the extensions that have a twin."""
    if not _built(name):
        pytest.skip(f"{name} is not compiled in this build")
    monkeypatch.setenv("WREATH_PURE", "1")
    resolved = _native.extension(name)
    if name in HONOURS_PURE:
        assert resolved is None, f"{name} has a pure twin and must yield to it"
    else:
        assert resolved is not None, f"{name} has no pure twin and must load anyway"


def test_the_pure_gate_is_re_read_on_every_call(monkeypatch: Any) -> None:
    """Not cached at import: `_select_protocol` is parametrized by monkeypatching.

    `tests/test_server_cancel_on_disconnect.py` drives both drivers in one
    process by setting the variable between calls. A loader that resolved once
    and cached would run that test's "pure" parameter against the native driver
    and pass while proving nothing.
    """
    if not _built("_server"):
        pytest.skip("_server is not compiled in this build")
    monkeypatch.setenv("WREATH_PURE", "1")
    assert _native.extension("_server") is None
    monkeypatch.delenv("WREATH_PURE")
    assert _native.extension("_server") is not None


def test_ignore_pure_reaches_the_compiled_code_in_either_mode(monkeypatch: Any) -> None:
    """The escape hatch replay and `wreath.xml` need, spelled once."""
    if not _built("_server"):
        pytest.skip("_server is not compiled in this build")
    monkeypatch.setenv("WREATH_PURE", "1")
    assert _native.extension("_server", ignore_pure=True) is not None


def test_an_unknown_extension_is_an_error_not_a_none() -> None:
    """A typo'd name must not read as "this build has not got it"."""
    with pytest.raises(AttributeError):
        _native.extension("_no_such_extension")


def test_replay_resolves_the_native_driver_under_wreath_pure(monkeypatch: Any) -> None:
    """Replay is native-first *on purpose*, and this is where that is written down.

    The pure HTTP/1 driver is the readable reference, not a performance peer;
    replaying a recording through it would make every replayed run report the
    timings of a framework Wreath is measured against rather than its own. So
    replay asks for the compiled driver in both modes -- unlike `wreath.server`,
    which honours the variable. Flip either half and this test says so.
    """
    if not _built("_server"):
        pytest.skip("_server is not compiled in this build")
    from wreath import replay, server

    monkeypatch.setenv("WREATH_PURE", "1")
    native_server = importlib.import_module("wreath._native._server")

    assert replay._default_protocol_cls() is native_server.Http1Protocol
    assert server._select_protocol() is not native_server.Http1Protocol
