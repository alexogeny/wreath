"""S3ObjectStore over a fake in-process transport (no network) + app.objects wiring.

Real S3/MinIO integration is gated on an env DSN and lives elsewhere; here we pin
the S3 REST + SigV4 signing behaviour against canned responses.
"""
from __future__ import annotations

import asyncio

from wreath.storage import ObjectPath, ObjectStat, S3ObjectStore, file_chunks


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
    from wreath.storage import LocalObjectStore

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
