"""API versioning: the ``version`` tag, header negotiation, and mounting."""

from __future__ import annotations

from wreath.request import Request
from wreath.versioning import VERSION_ATTR, VersionedRouter, negotiate_version, version


def test_version_decorator_tags():
    @version("2")
    async def handler(request):
        return None

    assert getattr(handler, VERSION_ATTR) == "2"


def test_negotiate_version():
    class Req:
        """Shaped like `Request`: `header(name, default)`, not a headers mapping."""

        def __init__(self, hdr):
            self._hdr = hdr

        def header(self, name, default=None):
            return self._hdr if name == "accept-version" and self._hdr else default

    supported = ("1", "2")
    assert negotiate_version(Req("2"), default="1", supported=supported) == "2"
    assert negotiate_version(Req("9"), default="1", supported=supported) == "1"  # unsupported
    assert negotiate_version(Req(None), default="1", supported=supported) == "1"  # absent
    assert negotiate_version(object(), default="1", supported=supported) == "1"  # no accessor


def test_negotiate_version_reads_a_real_request():
    """The regression the old blanket catch hid.

    `Request.headers` is a list of raw byte pairs with no `.get`, so reading it
    that way raised on every real request and the catch returned `default`.
    A dict-shaped double passed while nothing in production ever negotiated.
    """

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/x",
            "query_string": b"",
            "headers": [(b"accept-version", b"2")],
        },
        receive,
    )

    assert negotiate_version(request, default="1", supported=("1", "2")) == "2"


def test_versioned_router_mounts_prefixes():
    api = VersionedRouter()

    async def v1(request):
        return None

    async def v2(request):
        return None

    api.version("1").get("/llamas")(v1)
    api.version("2").get("/llamas")(v2)
    assert api.versions == ("1", "2")
    mounted = api.router()  # /v1/llamas and /v2/llamas
    assert mounted is not None
