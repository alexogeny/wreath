"""Dunelark ringing-station roster — the Tornado shapes that have a spelling.

A Tornado handler class is not one construct, it is a bundle of them, and the
bundle comes apart cleanly when the class inherits nothing but
`RequestHandler`. Each verb is a route function; the path comes from the regex
tuple that names the class, and the *names* of the path parameters come from
the verb's own parameters, which is why they are spelled out here rather than
taken positionally.

The patterns below are the two shapes that convert without a decision: a
literal prefix with `([0-9]+)` or `([^/]+)` captures, and the named-group form
`(?P<name>...)`. `self.write`, `self.get_argument`, `self.set_status`,
`HTTPError` and `self.redirect` each have one wreath spelling.

The inheritance chains, the sub-minute `PeriodicCallback`, `@gen.coroutine` and
the websocket handler are in `foreign/larkspur_roster/`, because each of those
is a decision rather than a rewrite.
"""

import json
from dataclasses import dataclass

import tornado.ioloop
import tornado.web
from tornado.options import define, options

define("ring_prefix", default="DL", help="scheme prefix stamped on every ring")
define("page_size", default=50, help="rows per roster page")


@dataclass
class Ringing:
    species: str
    ring: str
    grams: float


class HealthHandler(tornado.web.RequestHandler):
    def get(self):
        self.write({"status": "ok"})


class SessionHandler(tornado.web.RequestHandler):
    def get(self, session_id):
        self.write({"session": int(session_id), "prefix": options.ring_prefix})

    def delete(self, session_id):
        self.set_status(204)


class RingingHandler(tornado.web.RequestHandler):
    def get(self, session_id, ring):
        self.write({"session": int(session_id), "ring": ring})

    def post(self, session_id, ring):
        payload = json.loads(self.request.body)
        ringing = Ringing(**payload)
        self.set_status(201)
        self.write({"ring": ringing.ring, "grams": ringing.grams})


class RosterHandler(tornado.web.RequestHandler):
    def get(self):
        species = self.get_argument("species")
        limit = int(self.get_argument("limit", "50"))
        observer = self.get_query_argument("observer", None)
        self.write({"species": species, "limit": limit, "observer": observer})


class WeighHandler(tornado.web.RequestHandler):
    def post(self, session_id):
        grams = float(self.get_body_argument("grams"))
        recorder = self.get_body_argument("recorder", "unattributed")
        self.write({"grams": grams, "recorder": recorder})


class TraceHandler(tornado.web.RequestHandler):
    def get(self, session_id):
        trace = self.request.headers.get("X-Trace-Id")
        token = self.get_cookie("roster_session")
        self.set_header("X-Ring-Scheme", options.ring_prefix)
        self.write({"trace": trace, "token": token})


class ReleaseHandler(tornado.web.RequestHandler):
    def post(self, session_id):
        if session_id == "0":
            raise tornado.web.HTTPError(404)
        if session_id == "1":
            raise tornado.web.HTTPError(409, reason="session already closed")
        self.write({"released": session_id})


class LegacySessionHandler(tornado.web.RequestHandler):
    def get(self, session_id):
        self.redirect(f"/sessions/{session_id}", permanent=True)


class ExportHandler(tornado.web.RequestHandler):
    async def get(self, session_id):
        rows = await load_export(int(session_id))
        self.write({"rows": rows})


def make_app():
    application = tornado.web.Application([
        (r"/healthz", HealthHandler),
        (r"/sessions/([0-9]+)", SessionHandler),
        (r"/sessions/([0-9]+)/rings/([^/]+)", RingingHandler),
        (r"/roster", RosterHandler),
        (r"/sessions/([0-9]+)/weigh", WeighHandler),
        (r"/sessions/(?P<session_id>[0-9]+)/trace", TraceHandler),
        (r"/sessions/([0-9]+)/release", ReleaseHandler),
        (r"/legacy/sessions/([0-9]+)", LegacySessionHandler),
        (r"/sessions/([0-9]+)/export", ExportHandler),
    ])
    tornado.ioloop.PeriodicCallback(sweep_stale_sessions, 300_000).start()
    return application


def sweep_stale_sessions():
    raise NotImplementedError("wired up by the runner")


async def load_export(session_id):
    raise NotImplementedError("wired up by the runner")
