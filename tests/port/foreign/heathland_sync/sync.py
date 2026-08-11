"""Availability sync for a small lodging operator.

The two lines at the top change what every call below them means. `requests.get`
yields to the hub instead of blocking the process; `time.sleep` is a scheduling
point, not a stall; the lock guards a greenlet, not a thread. Nothing at any
call site says so.

A rewrite that reads this file and emits `async def` produces something that
passes its tests — the tests run one request at a time — and serialises under
load, because the database driver underneath is a C extension that never learned
to yield. That is the case for refusing rather than scoring.
"""

from gevent import monkey

monkey.patch_all()

import threading  # noqa: E402  -- after the patch, deliberately
import time  # noqa: E402

import requests  # noqa: E402
from bottle import Bottle, request  # noqa: E402

app = Bottle()

_rates: dict[str, float] = {}
# Greenlet-local under the patch above, though nothing here spells it that way.
_local = threading.local()
_lock = threading.Lock()

CHANNELS = ("meadowbrook", "fernhollow", "stonewell")


@app.get("/rates/<room>")
def read_rate(room):
    with _lock:
        return {"room": room, "rate": _rates.get(room, 0.0)}


@app.post("/rates/<room>")
def write_rate(room):
    rate = float(request.forms.get("rate", "0"))
    with _lock:
        _rates[room] = rate
    push_to_channels(room, rate)
    return {"ok": True}


def push_to_channels(room, rate):
    """One request per channel, one second apart.

    The sleep reads as a rate limiter. Under the patch it yields, so every
    greenlet in the fan-out wakes at roughly the same moment and the limit is
    whatever the scheduler decides.
    """
    for channel in CHANNELS:
        requests.post(
            f"https://{channel}.example/inventory",
            json={"room": room, "rate": rate},
            timeout=10,
        )
        time.sleep(1.0)


def current_session():
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session
