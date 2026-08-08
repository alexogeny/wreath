"""The one place that decides which implementation runs.

Exposes every compiled extension, or ``None`` when it is unavailable or
``WREATH_PURE=1`` requests the pure-Python twin. Facade modules in ``wreath.*``
import from here and fall back to ``wreath._pure``.

Three questions have to be answered to load one extension: does ``WREATH_PURE``
suppress this one, what happens when the build has not got it, and is the answer
re-read per call. They are answered here, once, per extension -- because when
they were answered at each call site instead, no two sites agreed, and one of
the disagreements was silent. ``wreath.replay`` resolved the native HTTP/1
driver under ``WREATH_PURE=1`` while ``wreath.server`` resolved the pure one, in
the mode whose whole purpose is holding the two to each other. That particular
divergence is *wanted* -- see ``ignore_pure`` -- but it is a declaration now,
not a consequence of which loader someone copied.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

#: Every extension `setup.py` builds, mapped to whether `WREATH_PURE=1`
#: suppresses it. A name absent from here is a typo, not a build without it.
#:
#: **True** means the extension has a pure-Python twin in `wreath._pure` that
#: `WREATH_PURE=1` selects instead, so the parity suite can hold the two to each
#: other. **False** means it is native by definition with nothing to fall back
#: to (AGENTS.md): gating `_reactor` or `_edge` would turn `timers="wheel"` and
#: `import wreath.edge` into failures in a mode with nothing else to offer them,
#: and `_flight`/`_http3` are the same shape -- the flight recorder's ring and
#: the HTTP/3 stack have no reference implementation. `wreath.reactor` and
#: `wreath.edge.headers` each raise a named error when their extension is absent.
_EXTENSIONS: dict[str, bool] = {
    "_core": True,
    "_client": True,
    "_postgres": True,
    "_server": True,
    "_reactor": False,
    "_edge": False,
    "_flight": False,
    "_http3": False,
}

def extension(name: str, *, ignore_pure: bool = False) -> Any | None:
    """One compiled extension, or `None` when this build has not got it.

    The gate is re-read on **every call**, which is what lets one process drive
    both implementations: `tests/test_server_cancel_on_disconnect.py`
    parametrizes native-versus-pure by setting the variable between calls to
    `wreath.server._select_protocol`. A loader that resolved once and cached
    would run that test's "pure" parameter against the native driver and pass
    while proving nothing. Re-reading costs a `sys.modules` hit, because
    `import_module` is a dict lookup after the first call.

    `ignore_pure=True` asks for the compiled code *whatever mode the process is
    in*. Two callers want that and both have a reason. `wreath.replay` is
    native-first because the pure driver is the readable reference rather than a
    performance peer, and replaying through it would report the timings of a
    framework Wreath is measured against instead of its own. `wreath.xml`'s
    `_require_native` needs it because its parity suite must reach the C parser
    in the very mode that hides it.

    An unknown `name` raises rather than returning `None`: a typo must not read
    as "this build has not got it".
    """
    honours_pure = _EXTENSIONS.get(name)
    if honours_pure is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if honours_pure and not ignore_pure and os.environ.get("WREATH_PURE"):
        return None
    try:
        return importlib.import_module(f"wreath._native.{name}")
    except ImportError:
        return None


# Any-typed: the compiled module is invisible to static analysis, and callers
# guard on ``_core is None`` before touching its attributes.
#
# `_core` is the one eager load: nearly every facade wants it, and it costs a
# dlopen. Everything else waits for a caller that means it -- `_client`'s module
# init alone imports `asyncio`, which brings `ssl`, `subprocess`, `logging`,
# `inspect` and `dataclasses` with it and measured at 74 ms of the 118 ms
# `import wreath._native._core` used to cost, charged to every subprocess and
# every xdist worker for a client most of them never open.
_core: Any = extension("_core")


def __getattr__(name: str) -> Any:
    """Load one optional extension on first reference, then cache it.

    Caching by assignment into the module globals is what makes this a one-time
    cost: `__getattr__` runs only when normal attribute lookup fails, so the
    second reference never reaches here. It also means a test may assign
    `wreath._native._client = None` and have that stick, the way it did when
    these were plain module-level names.

    Attribute access is therefore *import-time* semantics, and `extension()` is
    the call-time spelling. A caller that has to see a mid-process change to
    `WREATH_PURE` must use the function.
    """
    module = extension(name)
    globals()[name] = module
    return module
