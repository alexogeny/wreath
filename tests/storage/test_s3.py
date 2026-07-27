"""S3ObjectStore over a fake in-process transport (no network) + app.objects wiring.

Real S3/MinIO integration is gated on an env DSN and lives elsewhere; here we pin
the S3 REST + SigV4 signing behaviour against canned responses.
"""
from __future__ import annotations

import asyncio

import pytest

from wreath.objects import ObjectError, ObjectPath, ObjectStat, S3ObjectStore, file_chunks


class FakeResp:
    def __init__(self, status: int, headers=(), body: bytes = b"") -> None:
        self.status = status
        self.headers = tuple(headers)
        self.body = body

    def header(self, name: bytes) -> bytes | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None


class FakeClient:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []

    async def request(self, method, target, *, headers=(), body=b""):
        hdrs = {k.decode("ascii").lower(): v.decode("latin-1") for k, v in headers}
        self.calls.append((method, target, hdrs, bytes(body)))
        return self._handler(method, target, bytes(body))


def _store(handler, **kw):
    client = FakeClient(handler)
    store = S3ObjectStore(
        client, bucket="b", region="us-east-1", access_key="AKIAEXAMPLE",
        secret_key="secretkey", host="b.s3.us-east-1.amazonaws.com", **kw,
    )
    return store, client


def test_write_and_read_and_delete():
    def handler(method, target, body):
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"abc123"')])
        if method == "GET":
            return FakeResp(200, body=b"hello llamas")
        if method == "DELETE":
            return FakeResp(204)
        return FakeResp(400)

    store, client = _store(handler)

    async def go():
        stat = await store.write("reports/q3.csv", b"hello llamas", content_type="text/csv")
        assert isinstance(stat, ObjectStat) and stat.etag == "abc123" and stat.size == 12
        # the PUT was signed + hashed
        m, target, hdrs, body = client.calls[0]
        assert m == "PUT" and target == "/reports/q3.csv"
        assert "authorization" in hdrs and hdrs["authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "x-amz-content-sha256" in hdrs and "x-amz-date" in hdrs
        assert await store.read("reports/q3.csv") == b"hello llamas"
        await store.delete("reports/q3.csv")
        assert client.calls[-1][0] == "DELETE"

    asyncio.run(go())


def test_stat_and_exists():
    def handler(method, target, body):
        if "missing" in target:
            return FakeResp(404)
        return FakeResp(200, [
            (b"content-length", b"42"), (b"etag", b'"e"'),
            (b"content-type", b"application/json"),
            (b"last-modified", b"Tue, 15 Nov 2022 12:45:26 GMT"),
        ])

    store, _ = _store(handler)

    async def go():
        st = await store.stat("a.json")
        assert st.size == 42 and st.etag == "e" and st.content_type == "application/json"
        assert st.last_modified is not None
        assert await store.exists("a.json") is True
        assert await store.exists("missing") is False

    asyncio.run(go())


def test_list_paginates():
    page1 = (
        b'<?xml version="1.0"?><ListBucketResult'
        b' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>true</IsTruncated><NextContinuationToken>TOK</NextContinuationToken>"
        b"<Contents><Key>a.txt</Key><Size>3</Size><ETag>&quot;e1&quot;</ETag></Contents>"
        b"</ListBucketResult>"
    )
    page2 = (
        b'<?xml version="1.0"?><ListBucketResult'
        b' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>false</IsTruncated>"
        b"<Contents><Key>b.txt</Key><Size>5</Size><ETag>&quot;e2&quot;</ETag></Contents>"
        b"</ListBucketResult>"
    )

    def handler(method, target, body):
        return FakeResp(200, body=page2 if "continuation-token=TOK" in target else page1)

    store, _ = _store(handler)

    async def go():
        keys = [o.key async for o in store.list(prefix="")]
        assert keys == ["a.txt", "b.txt"]

    asyncio.run(go())


def test_multipart_write_stream():
    state = {"upload_id": None, "parts": 0, "completed": False}

    def handler(method, target, body):
        if method == "POST" and "uploads=" in target and "uploadId" not in target:
            state["upload_id"] = "UP1"
            return FakeResp(200, body=(
                b'<InitiateMultipartUploadResult'
                b' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                b"<UploadId>UP1</UploadId></InitiateMultipartUploadResult>"
            ))
        if method == "PUT" and "partNumber" in target:
            state["parts"] += 1
            return FakeResp(200, [(b"etag", f'"p{state["parts"]}"'.encode())])
        if method == "POST" and "uploadId=UP1" in target:
            state["completed"] = True
            return FakeResp(200, body=b"<CompleteMultipartUploadResult/>")
        if method == "HEAD":
            return FakeResp(200, [(b"content-length", b"6291457"), (b"etag", b'"final"')])
        return FakeResp(400, body=target.encode())

    store, _ = _store(handler, part_size=5 * 1024 * 1024)

    async def go():
        # 6 MiB + 1 byte forces a 5 MiB part + a small final part → real multipart
        payload = b"x" * (5 * 1024 * 1024 + 1)
        stat = await store.write_stream("big.bin", file_chunks(payload, chunk_size=1 << 20))
        assert state["upload_id"] == "UP1"
        assert state["parts"] == 2  # one full part + the remainder
        assert state["completed"] is True
        assert stat.key == "big.bin"

    asyncio.run(go())


def test_small_write_stream_is_single_put():
    def handler(method, target, body):
        assert "uploadId" not in target and "uploads" not in target  # never multipart
        return FakeResp(200, [(b"etag", b'"one"')])

    store, client = _store(handler)

    async def go():
        stat = await store.write_stream("small.txt", file_chunks(b"tiny"))
        assert stat.etag == "one"
        assert all(c[0] == "PUT" for c in client.calls)

    asyncio.run(go())


def test_read_stream_windows():
    obj = b"0123456789ABCDEF"  # 16 bytes == 4 windows of 4 → exercises the 416 EOF path
    store, client = _store(lambda *a: FakeResp(400), window=4)

    def rh(method, target, body):
        rng = client.calls[-1][2].get("range", "")
        lo, hi = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        piece = obj[lo:hi + 1]
        return FakeResp(206 if piece else 416, body=piece)

    client._handler = rh

    async def go():
        got = b""
        async for part in store.read_stream("f"):
            got += part
        assert got == obj

    asyncio.run(go())


def test_presign_url_has_signature():
    store, _ = _store(lambda *a: FakeResp(200))
    url = store.url("reports/q3.csv", expires=900)
    assert url.startswith("https://b.s3.us-east-1.amazonaws.com/reports/q3.csv?")
    assert "X-Amz-Signature=" in url and "X-Amz-Credential=" in url
    assert "X-Amz-Expires=900" in url


def test_path_ergonomics():
    store, _ = _store(lambda *a: FakeResp(200))
    p = store.path("org/acme") / "state.json"
    assert isinstance(p, ObjectPath) and p.key == "org/acme/state.json"


# -- app.objects wiring -------------------------------------------------------
def test_app_objects_local_roundtrip(tmp_path):
    from wreath import Wreath
    from wreath.objects import LocalObjectStore

    app = Wreath()
    store = app.objects("blobs", backend="local", root=str(tmp_path))
    assert isinstance(store, LocalObjectStore)
    assert app.state.objects_blobs is store

    async def go():
        await store.write("a/b.txt", b"data")
        assert await store.read("a/b.txt") == b"data"

    asyncio.run(go())
    with_dup = False
    try:
        app.objects("blobs", backend="local", root=str(tmp_path))
    except ValueError:
        with_dup = True
    assert with_dup


def test_app_objects_s3_registration():
    from wreath import Wreath

    app = Wreath()
    store = app.objects(
        "assets", backend="s3", bucket="ev-assets", region="ap-southeast-2",
        access_key="AKIA", secret_key="sk",
    )
    assert isinstance(store, S3ObjectStore)
    assert app.state.objects_assets is store
    assert "__objects_assets" in app._http_clients  # lifespan-managed client
    # repr never leaks credentials
    assert repr(store) == "S3ObjectStore(bucket='ev-assets', region='ap-southeast-2')"


def test_app_objects_missing_creds_raises(monkeypatch):
    from wreath import Wreath

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    app = Wreath()
    raised = False
    try:
        app.objects("x", backend="s3", bucket="b", region="r")  # no creds, no env
    except ValueError:
        raised = True
    assert raised


def test_a_failed_abort_is_counted_not_swallowed():
    """A multipart upload that can neither complete nor abort leaves orphans.

    `_abort` runs while a more interesting exception is propagating, so it must
    not raise -- but it used to `pass`, which meant parts already stored in S3
    stopped belonging to any object and nothing recorded that it had happened.
    They accrue storage charges until a lifecycle rule reaps them, and the only
    signal was the bill.
    """
    from wreath.http_client import ConnectError

    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":                    # complete -> fails
            return FakeResp(500, [], b"nope")
        if method == "DELETE":                  # abort -> transport is gone too
            raise ConnectError("connection refused")
        raise AssertionError(method)

    store, _ = _store(handler, part_size=5 * 1024 * 1024)

    async def chunks():
        yield b"x" * (5 * 1024 * 1024)
        yield b"y" * 16

    assert store.orphaned_uploads == 0
    with pytest.raises(ObjectError):
        asyncio.run(store.write_stream("k", chunks()))
    assert store.orphaned_uploads == 1, "a failed abort must leave a countable trace"


def test_a_bug_in_abort_is_not_hidden_behind_a_transport_failure():
    """Only transport errors are absorbed; a programming error still surfaces."""
    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":
            return FakeResp(500, [], b"nope")
        if method == "DELETE":
            raise TypeError("bug in _abort")
        raise AssertionError(method)

    store, _ = _store(handler, part_size=5 * 1024 * 1024)

    async def chunks():
        yield b"x" * (5 * 1024 * 1024)
        yield b"y" * 16

    with pytest.raises(TypeError, match="bug in _abort"):
        asyncio.run(store.write_stream("k", chunks()))
    assert store.orphaned_uploads == 0


# -- multipart failure handling ----------------------------------------------
def _multipart_body():
    """5 MiB + a tail: enough to force an initiate, a part, and a final part."""
    async def chunks():
        yield b"x" * (5 * 1024 * 1024)
        yield b"y" * 16

    return chunks()


def test_a_failed_part_aborts_the_upload():
    """A part S3 rejects leaves an open upload unless the abort covers the whole window.

    Only `_complete` used to be wrapped, so a `_put_part` failure propagated with
    the upload still open in the bucket -- billable parts belonging to no object,
    and `orphaned_uploads` not incremented either, so the only signal was the
    invoice.
    """
    seen = {"aborted": False}

    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(500, [], b"part rejected")
        if method == "DELETE":
            seen["aborted"] = True
            return FakeResp(204)
        raise AssertionError(method)

    store, client = _store(handler, part_size=5 * 1024 * 1024)
    with pytest.raises(ObjectError):
        asyncio.run(store.write_stream("k", _multipart_body()))
    assert seen["aborted"], "a part that fails must abort the multipart upload"
    assert any(m == "DELETE" and "uploadId=u1" in t for m, t, _h, _b in client.calls)
    assert store.orphaned_uploads == 0  # the abort worked, so nothing was orphaned


def test_a_failed_part_whose_abort_also_fails_is_counted():
    from wreath.http_client import ConnectError

    attempts = {"delete": 0}

    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(500, [], b"part rejected")
        if method == "DELETE":
            attempts["delete"] += 1
            raise ConnectError("connection refused")
        raise AssertionError(method)

    store, _ = _store(handler, part_size=5 * 1024 * 1024)
    with pytest.raises(ObjectError):
        asyncio.run(store.write_stream("k", _multipart_body()))
    assert attempts["delete"] == 1
    assert store.orphaned_uploads == 1


def test_a_chunk_iterator_that_raises_aborts_the_upload():
    """The bytes can fail before S3 does; the upload is open either way."""
    seen = {"aborted": False}

    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "DELETE":
            seen["aborted"] = True
            return FakeResp(204)
        raise AssertionError(method)

    store, _ = _store(handler, part_size=5 * 1024 * 1024)

    async def chunks():
        yield b"x" * (5 * 1024 * 1024)
        raise ZeroDivisionError("the producer blew up")

    with pytest.raises(ZeroDivisionError):
        asyncio.run(store.write_stream("k", chunks()))
    assert seen["aborted"]


def test_an_abort_s3_refuses_is_counted_too():
    """A 403 on the abort orphans exactly as much as a dropped connection does."""
    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":
            return FakeResp(500, [], b"nope")
        if method == "DELETE":
            return FakeResp(403, [], b"<Error>AccessDenied</Error>")
        raise AssertionError(method)

    store, _ = _store(handler, part_size=5 * 1024 * 1024)
    with pytest.raises(ObjectError):
        asyncio.run(store.write_stream("k", _multipart_body()))
    assert store.orphaned_uploads == 1


# -- content type -------------------------------------------------------------
def test_write_sends_and_signs_the_content_type():
    """The stat used to claim a media type the bucket was never told about."""
    store, client = _store(lambda *a: FakeResp(200, [(b"etag", b'"abc"')]))
    stat = asyncio.run(store.write("a.csv", b"x,y", content_type="text/csv"))
    assert stat.content_type == "text/csv"
    _m, _t, hdrs, _b = client.calls[0]
    assert hdrs.get("content-type") == "text/csv"
    assert "SignedHeaders=content-type;host" in hdrs["authorization"]


def test_write_without_a_content_type_sends_no_header():
    store, client = _store(lambda *a: FakeResp(200, [(b"etag", b'"abc"')]))
    asyncio.run(store.write("a.bin", b"x"))
    assert "content-type" not in client.calls[0][2]


def test_multipart_initiate_sends_the_content_type():
    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":
            return FakeResp(200, [], b"<ok/>")
        if method == "HEAD":
            return FakeResp(200, [(b"content-length", b"16"), (b"etag", b'"f"')])
        raise AssertionError(method)

    store, client = _store(handler, part_size=5 * 1024 * 1024)
    asyncio.run(store.write_stream("k.zip", _multipart_body(), content_type="application/zip"))
    initiate = next(c for c in client.calls if c[0] == "POST" and "uploads" in c[1])
    assert initiate[2].get("content-type") == "application/zip"


def test_stat_survives_a_non_ascii_content_type():
    headers = [(b"content-length", b"1"), (b"etag", b'"e"'),
               (b"content-type", b'text/plain; name="caf\xe9.txt"')]
    store, _ = _store(lambda *a: FakeResp(200, headers))
    st = asyncio.run(store.stat("a.txt"))
    assert st.content_type is not None and "caf" in st.content_type


# -- constructor --------------------------------------------------------------
def test_url_secret_is_refused_rather_than_ignored():
    """It was accepted and never assigned: configuration that silently did nothing."""
    with pytest.raises(TypeError, match="url_secret"):
        _store(lambda *a: FakeResp(200), url_secret=b"x" * 32)


def test_an_abort_answered_404_is_not_an_orphan():
    """`NoSuchUpload` means there is nothing left to reclaim, so nothing to alarm about."""
    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":
            return FakeResp(500, [], b"nope")
        if method == "DELETE":
            return FakeResp(404, [], b"<Error>NoSuchUpload</Error>")
        raise AssertionError(method)

    store, _ = _store(handler, part_size=5 * 1024 * 1024)
    with pytest.raises(ObjectError):
        asyncio.run(store.write_stream("k", _multipart_body()))
    assert store.orphaned_uploads == 0
