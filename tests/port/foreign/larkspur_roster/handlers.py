"""Larkspur ringing station — the Tornado shapes that stay refused.

The companion to `corpus/dunelark_roster/`. Each stanza is one reason a handler
class does *not* come apart into route functions:

* the base-class chain — `prepare` runs before every verb on every descendant,
  and `get_current_user` decides what `@authenticated` does. What a route does
  is not written in the class that declares it.
* `self.get_argument` inside a **write** verb — Tornado merges the query string
  and the body into one argument namespace, so this read has two possible
  sources and `Query()` would be a widening. Only `get_query_argument` and
  `get_body_argument` say which.
* a sub-minute `PeriodicCallback` — `_jobcore`'s cron grammar is "minute hour
  day-of-month month day-of-week", so thirty seconds is below the smallest
  period `jobs.schedule` can express.
* `@gen.coroutine` — eager, so a caller that does not await still gets the
  effect; the mechanical `async def` rewrite loses it silently.
* `WebSocketHandler` with a class-level registry — `wreath.websocket` hands the
  handler one `WebSocket` and owns the connection set itself.
* `write_error` — a per-handler error page, where `add_status_handler` is
  per-application.
* an optional capture group — `([0-9]+)?` is two routes, and which one the
  verb is serving is decided by whether its parameter is None.
"""

import tornado.ioloop
import tornado.web
import tornado.websocket
from tornado import gen


class BaseHandler(tornado.web.RequestHandler):
    def prepare(self):
        self.station = self.get_query_argument("station", "larkspur")

    def get_current_user(self):
        return self.get_secure_cookie("ringer")

    def write_error(self, status_code, **kwargs):
        self.write({"error": status_code, "station": self.station})


class AuditedMixin:
    def on_finish(self):
        record_audit(self.request.path, self.current_user)


class CatchHandler(AuditedMixin, BaseHandler):
    @tornado.web.authenticated
    def post(self, catch_id=None):
        ring = self.get_argument("ring")
        self.write({"catch": catch_id, "ring": ring, "station": self.station})

    @gen.coroutine
    def put(self, catch_id=None):
        yield self.reconcile(catch_id)
        self.set_status(202)

    def reconcile(self, catch_id):
        raise NotImplementedError("the catch store is injected by the runner")


class ReportHandler(BaseHandler):
    def get(self):
        self.render("report.html", station=self.station)


class RosterSocket(tornado.websocket.WebSocketHandler):
    listeners = set()

    def open(self, station):
        RosterSocket.listeners.add(self)

    def on_message(self, message):
        for listener in RosterSocket.listeners:
            listener.write_message(message)

    def on_close(self):
        RosterSocket.listeners.discard(self)


def make_app():
    application = tornado.web.Application([
        (r"/catches/([0-9]+)?", CatchHandler),
        (r"/report", ReportHandler),
        (r"/stations/([^/]+)/live", RosterSocket),
    ])
    tornado.ioloop.PeriodicCallback(sweep_listeners, 30_000).start()
    return application


def sweep_listeners():
    RosterSocket.listeners = {s for s in RosterSocket.listeners if s.ws_connection}


def record_audit(path, user):
    raise NotImplementedError("wired up by the runner")
