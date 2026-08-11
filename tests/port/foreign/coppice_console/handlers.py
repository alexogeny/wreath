"""The coppice rotation console.

Behaviour here is inherited rather than declared. What a route does depends on
which bases its class lists — ``prepare`` runs before every verb on the whole
chain, and ``@authenticated`` redirects rather than raising — and the path it
answers on lives in a regex tuple that names the class from somewhere else. The
socket keeps its subscribers in a class attribute, and ``@gen.coroutine``
executes eagerly, so rewriting it to ``async def`` turns a call nobody awaits
from a working effect into a missing one.
"""
import tornado.options
import tornado.web
import tornado.websocket
from tornado import gen
from tornado.ioloop import PeriodicCallback
from tornado.options import options

tornado.options.define("rotation_years", default=15, help="length of the coppice cycle")


class BaseHandler(tornado.web.RequestHandler):
    def prepare(self):
        self.coupe = self.get_argument("coupe", "north")

    def get_current_user(self):
        return self.get_secure_cookie("forester")


class CoupeHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, coupe_id):
        self.write({"coupe": coupe_id, "rotation": options.rotation_years})

    @gen.coroutine
    def post(self, coupe_id):
        yield self.record(coupe_id)
        self.set_status(202)

    def record(self, coupe_id):
        raise NotImplementedError("the coupe store is injected by the runner")


class CoupeSocket(tornado.websocket.WebSocketHandler):
    listeners = set()

    def open(self, coupe_id):
        CoupeSocket.listeners.add(self)

    def on_message(self, message):
        for listener in CoupeSocket.listeners:
            listener.write_message(message)

    def on_close(self):
        CoupeSocket.listeners.discard(self)


def make_app():
    application = tornado.web.Application([
        (r"/coupes/([0-9]+)", CoupeHandler),
        (r"/coupes/([0-9]+)/live", CoupeSocket),
    ])
    PeriodicCallback(sweep_closed_listeners, 30_000).start()
    return application


def sweep_closed_listeners():
    CoupeSocket.listeners = {s for s in CoupeSocket.listeners if s.ws_connection}
