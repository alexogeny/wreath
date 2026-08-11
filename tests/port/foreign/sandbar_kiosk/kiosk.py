"""A kiosk that prints tide tables in a harbour hut.

Two things make this tree unportable and they compound. Bottle spells its route
decorators exactly the way FastAPI does, so the routes look translatable and are
not — the handler reads a module-level request proxy and returns a body rather
than a response. And ``patch_all()`` above reinterprets every blocking call
below it: the pool bound is the only backpressure there is, the ``Timeout``
derives from ``BaseException`` so no ``except Exception`` catches it, and
psycopg2 is a C extension that never yields at all, so one slow query stalls
every greenlet in the worker.
"""
from gevent import monkey

monkey.patch_all()

import threading

import psycopg2
import requests
from bottle import Bottle, request, response
from gevent import Timeout, spawn
from gevent.pool import Pool

kiosk = Bottle()
printers = Pool(8)
local = threading.local()
session = requests.Session()


@kiosk.get("/tides/<harbour>")
def tide_table(harbour):
    with Timeout(5):
        rows = _read_tides(harbour)
    response.content_type = "application/json"
    return {"harbour": harbour, "rows": rows}


@kiosk.post("/print")
def queue_print():
    harbour = request.forms.get("harbour")
    printers.spawn(_print_table, harbour)
    return {"queued": harbour}


def _read_tides(harbour):
    if not hasattr(local, "connection"):
        local.connection = psycopg2.connect("dbname=sandbar")
    with local.connection.cursor() as cursor:
        cursor.execute("SELECT at, height FROM tide WHERE harbour = %s", (harbour,))
        return cursor.fetchall()


def drain_print_queue():
    printers.join(timeout=10)


def _print_table(harbour):
    spawn(session.post, "http://printer.sandbar.invalid/jobs", json={"harbour": harbour})
