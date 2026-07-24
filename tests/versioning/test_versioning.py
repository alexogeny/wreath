"""API versioning: the ``version`` tag, header negotiation, and mounting."""

from __future__ import annotations

from wreath.versioning import VERSION_ATTR, VersionedRouter, negotiate_version, version


def test_version_decorator_tags():
    @version("2")
    async def handler(request):
        return None

    assert getattr(handler, VERSION_ATTR) == "2"


def test_negotiate_version():
    class Req:
        def __init__(self, hdr):
            self.headers = {"accept-version": hdr} if hdr is not None else {}

    supported = ("1", "2")
    assert negotiate_version(Req("2"), default="1", supported=supported) == "2"
    assert negotiate_version(Req("9"), default="1", supported=supported) == "1"  # unsupported
    assert negotiate_version(Req(None), default="1", supported=supported) == "1"  # absent
    assert negotiate_version(object(), default="1", supported=supported) == "1"  # no headers


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
