"""Evaporation-pond telemetry, in two generations of Tornado.

Routing is a list of regex tuples, so a path is a pattern and its captures
arrive as positional string arguments. Behaviour lives in a class hierarchy:
which mixins a handler inherits decides whether it authenticates, and in what
order.

The two coroutine generations sit side by side. `@gen.coroutine` with `yield`
predates `async def`, executes eagerly, and can be called without being awaited
— which half-works, right up until someone translates it mechanically and the
call becomes a coroutine nobody awaits.
"""

import tornado.web
from tornado import gen


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        # A synchronous database call on every request, inside the IOLoop.
        token = self.get_secure_cookie("session")
        return self.application.db.user_for_token(token) if token else None

    def write_error(self, status_code, **kwargs):
        self.finish({"error": status_code})


class ReadingsMixin:
    def readings(self, pond_id, since):
        return self.application.db.readings(int(pond_id), since)


class OperatorOnlyMixin:
    def prepare(self):
        user = self.get_current_user()
        if user is None or not user.get("operator"):
            raise tornado.web.HTTPError(403)


class PondHandler(ReadingsMixin, BaseHandler):
    def get(self, pond_id):
        self.write({"pond": int(pond_id), "readings": self.readings(pond_id, None)})


class SampleHandler(OperatorOnlyMixin, ReadingsMixin, BaseHandler):
    @gen.coroutine
    def post(self, pond_id, probe_id):
        body = tornado.escape.json_decode(self.request.body)
        yield self.application.db.record(int(pond_id), int(probe_id), body["value"])
        # Not yielded. Under gen.coroutine it runs anyway.
        self.audit(pond_id, body)
        self.write({"ok": True})

    @gen.coroutine
    def audit(self, pond_id, body):
        yield self.application.db.audit("sample", pond_id, body)


class ExportHandler(BaseHandler):
    async def get(self, pond_id):
        rows = await self.application.db.export(int(pond_id))
        self.set_header("Content-Type", "text/csv")
        for row in rows:
            self.write(f"{row['taken_at']},{row['value']}\n")


def make_app(db):
    application = tornado.web.Application(
        [
            (r"/pond/([0-9]+)", PondHandler),
            (r"/pond/([0-9]+)/probe/([0-9]+)/sample", SampleHandler),
            (r"/pond/([0-9]+)/export", ExportHandler),
        ]
    )
    application.db = db
    return application
