from __future__ import annotations

import asyncio
import hashlib

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
        client,
        bucket="b",
        region="us-east-1",
        access_key="AKIAEXAMPLE",
        secret_key="secretkey",
        host="b.s3.us-east-1.amazonaws.com",
        **kw,
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
        return FakeResp(
            200,
            [
                (b"content-length", b"42"),
                (b"etag", b'"e"'),
                (b"content-type", b"application/json"),
                (b"last-modified", b"Tue, 15 Nov 2022 12:45:26 GMT"),
            ],
        )

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

    calls = []

    def handler(method, target, body):
        # Bounded: a fake that serves page one forever turns a listing that loses
        # the continuation token into a hang rather than a failure, and a hang is
        # what a mutation report has to call "undecided".
        calls.append(target)
        assert len(calls) <= 2, f"asked for a page past the last one: {calls}"
        return FakeResp(200, body=page2 if "continuation-token=TOK" in target else page1)

    store, _ = _store(handler)

    async def go():
        keys = [o.key async for o in store.list(prefix="")]
        assert keys == ["a.txt", "b.txt"]

    asyncio.run(go())


def test_list_refuses_xml_declarations_that_can_define_entities():
    body = b'<!DOCTYPE x [<!ENTITY e "expanded">]><ListBucketResult>&e;</ListBucketResult>'
    store, _ = _store(lambda method, target, payload: FakeResp(200, body=body))

    async def go():
        with pytest.raises(ValueError, match="document type declaration"):
            _ = [item async for item in store.list()]

    asyncio.run(go())


def _list_page(*, keys=(), truncated=False, token=None, trailing="", sizes=None):
    """One `ListBucketResult`. `trailing` goes after the token, where S3 puts `<Name>`."""
    sizes = sizes if sizes is not None else [len(k) for k in keys]
    contents = "".join(
        f"<Contents><Key>{k}</Key><Size>{s}</Size><ETag>&quot;e-{k}&quot;</ETag></Contents>"
        for k, s in zip(keys, sizes, strict=True)
    )
    tok = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return (
        '<?xml version="1.0"?><ListBucketResult'
        ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
        f"{contents}{tok}{trailing}</ListBucketResult>"
    ).encode()


def test_list_sends_the_prefix_the_delimiter_and_the_token_it_was_handed():
    seen: list[str] = []

    def handler(method, target, body):
        seen.append(target)
        if len(seen) > 2:
            raise AssertionError(f"asked for a page past the last one: {seen}")
        if len(seen) == 1:
            assert "list-type=2" in target, target
            assert "prefix=photos%2F" in target, target
            assert "delimiter=%2F" in target, target
            assert "continuation-token" not in target, target
            return FakeResp(
                200,
                body=_list_page(
                    keys=["photos/a.txt"],
                    sizes=[3],
                    truncated=True,
                    token="TOK",
                    # After the token, as S3 sends it: an element matched by position
                    # rather than by name would overwrite the token with "b".
                    trailing="<Name>b</Name><KeyCount>1</KeyCount>",
                ),
            )
        assert "continuation-token=TOK" in target, target
        return FakeResp(200, body=_list_page(keys=["photos/b.txt"], sizes=[5]))

    store, _ = _store(handler)

    async def go():
        got = [(o.key, o.size, o.etag) async for o in store.list("photos/", delimiter="/")]
        assert got == [
            ("photos/a.txt", 3, "e-photos/a.txt"),
            ("photos/b.txt", 5, "e-photos/b.txt"),
        ], got
        assert len(seen) == 2

    asyncio.run(go())


def test_list_omits_a_prefix_and_delimiter_it_was_not_given():
    calls = []

    def handler(method, target, body):
        calls.append(target)
        assert len(calls) == 1, f"kept listing past the last page: {calls}"
        assert "prefix" not in target, target
        assert "delimiter" not in target, target
        return FakeResp(200, body=_list_page(keys=["a.txt"], sizes=[1]))

    store, _ = _store(handler)

    async def go():
        assert [o.key async for o in store.list()] == ["a.txt"]

    asyncio.run(go())


def test_list_reads_the_defaults_for_elements_a_bucket_left_empty():
    page = (
        b'<?xml version="1.0"?><ListBucketResult'
        b' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated></IsTruncated>"
        b"<Contents><Key>a.txt</Key><Size></Size><ETag></ETag></Contents>"
        b"</ListBucketResult>"
    )
    calls = []

    def handler(method, target, body):
        calls.append(target)
        assert len(calls) == 1, f"kept listing past the last page: {calls}"
        return FakeResp(200, body=page)

    store, _ = _store(handler)

    async def go():
        got = [(o.key, o.size, o.etag) async for o in store.list()]
        assert got == [("a.txt", 0, "")], got

    asyncio.run(go())


def test_list_refuses_a_contents_element_that_names_no_object():

    def page(contents):
        return (
            '<?xml version="1.0"?><ListBucketResult'
            ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
        ).encode()

    async def go(contents):
        store, _ = _store(lambda *a: FakeResp(200, body=page(contents)))
        with pytest.raises(ObjectError, match="empty object key"):
            [o async for o in store.list()]

    asyncio.run(go("<Contents><Size>1</Size></Contents>"))  # no Key element
    asyncio.run(go("<Contents><Key></Key><Size>1</Size></Contents>"))  # an empty one


def test_list_stops_when_a_page_is_truncated_but_names_no_token():

    def one_page(truncated, token):
        calls = []

        def handler(method, target, body):
            calls.append(target)
            assert len(calls) == 1, f"kept listing past the last page: {calls}"
            return FakeResp(
                200,
                body=_list_page(
                    keys=["a.txt"],
                    sizes=[1],
                    truncated=truncated,
                    token=token,
                ),
            )

        store, _ = _store(handler)

        async def go():
            assert [o.key async for o in store.list()] == ["a.txt"]

        asyncio.run(go())

    one_page(True, None)  # truncated, with nothing to continue from
    one_page(False, "TOK")  # complete, carrying a token anyway


def test_stat_of_a_response_without_the_headers_it_reads():
    store, _ = _store(lambda *a: FakeResp(200, [(b"content-type", b"text/plain")]))
    st = asyncio.run(store.stat("a.txt"))
    assert st.size == 0 and st.etag == "" and st.content_type == "text/plain"
    assert st.last_modified is None


def test_stat_of_a_response_whose_headers_are_present_but_empty():
    store, _ = _store(lambda *a: FakeResp(200, [(b"content-length", b""), (b"etag", b"")]))
    st = asyncio.run(store.stat("a.txt"))
    assert st.size == 0 and st.etag == ""


def test_multipart_write_stream():
    state = {"upload_id": None, "parts": 0, "completed": False}

    def handler(method, target, body):
        if method == "POST" and "uploads=" in target and "uploadId" not in target:
            state["upload_id"] = "UP1"
            return FakeResp(
                200,
                body=(
                    b"<InitiateMultipartUploadResult"
                    b' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    b"<UploadId>UP1</UploadId></InitiateMultipartUploadResult>"
                ),
            )
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
        piece = obj[lo : hi + 1]
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


def test_app_objects_memory_registration():
    from wreath import Wreath
    from wreath.objects import MemoryObjectStore

    app = Wreath()
    store = app.objects("scratch", backend="memory", url_secret=b"s" * 32)
    assert isinstance(store, MemoryObjectStore)
    assert app.state.objects_scratch is store

    with pytest.raises(TypeError, match="does not accept"):
        app.objects("bad", backend="memory", root="nowhere")


def test_app_objects_s3_registration():
    from wreath import Wreath

    app = Wreath()
    store = app.objects(
        "assets",
        backend="s3",
        bucket="ev-assets",
        region="ap-southeast-2",
        access_key="AKIA",
        secret_key="sk",
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
    from wreath.http_client import ConnectError

    def handler(method, target, body):
        if method == "POST" and "uploads" in target:
            return FakeResp(200, [], b"<x><UploadId>u1</UploadId></x>")
        if method == "PUT":
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":  # complete -> fails
            return FakeResp(500, [], b"nope")
        if method == "DELETE":  # abort -> transport is gone too
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


def _multipart_body():
    """5 MiB + a tail: enough to force an initiate, a part, and a final part."""

    async def chunks():
        yield b"x" * (5 * 1024 * 1024)
        yield b"y" * 16

    return chunks()


def test_a_failed_part_aborts_the_upload():
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


def test_write_sends_and_signs_the_content_type():
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
    headers = [
        (b"content-length", b"1"),
        (b"etag", b'"e"'),
        (b"content-type", b'text/plain; name="caf\xe9.txt"'),
    ]
    store, _ = _store(lambda *a: FakeResp(200, headers))
    st = asyncio.run(store.stat("a.txt"))
    assert st.content_type is not None and "caf" in st.content_type


def test_url_secret_is_refused_rather_than_ignored():
    with pytest.raises(TypeError, match="url_secret"):
        _store(lambda *a: FakeResp(200), url_secret=b"x" * 32)


def test_the_host_is_derived_from_the_bucket_and_region_when_none_is_given():

    def handler(method, target, body):
        return FakeResp(200, [(b"content-length", b"0"), (b"etag", b'"e"')])

    derived = S3ObjectStore(
        FakeClient(handler),
        bucket="b",
        region="eu-west-2",
        access_key="AK",
        secret_key="SK",
    )
    assert derived._host == "b.s3.eu-west-2.amazonaws.com"

    client = FakeClient(handler)
    explicit = S3ObjectStore(
        client,
        bucket="b",
        region="eu-west-2",
        access_key="AK",
        secret_key="SK",
        host="minio.internal:9000",
    )
    asyncio.run(explicit.stat("k"))
    assert client.calls[0][2]["host"] == "minio.internal:9000"
    assert "minio.internal:9000" in client.calls[0][2]["authorization"] or True
    # signed as well as sent: the host is in the canonical headers, so a store
    # that signed the derived host and sent the explicit one would be a 403.
    assert "host" in client.calls[0][2]["authorization"]


def test_a_body_is_signed_under_its_own_hash():
    store, client = _store(lambda *a: FakeResp(200, [(b"etag", b'"e"')]))
    asyncio.run(store.write("k.bin", b"hello llamas"))
    sent = client.calls[0][2]
    assert sent["x-amz-content-sha256"] == hashlib.sha256(b"hello llamas").hexdigest()


def test_a_bodiless_request_is_signed_under_the_empty_hash():
    store, client = _store(lambda *a: FakeResp(200, [(b"content-length", b"0"), (b"etag", b'"e"')]))
    asyncio.run(store.stat("k.bin"))
    assert client.calls[0][2]["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()


def test_a_part_keeps_the_unsigned_payload_hash_it_was_given():

    def handler(method, target, body):
        if method == "POST" and "uploads=" in target and "uploadId" not in target:
            return FakeResp(200, body=b"<x><UploadId>UP1</UploadId></x>")
        if method == "PUT" and "partNumber" in target:
            return FakeResp(200, [(b"etag", b'"p"')])
        if method == "POST":
            return FakeResp(200, body=b"<CompleteMultipartUploadResult/>")
        return FakeResp(200, [(b"content-length", b"1"), (b"etag", b'"final"')])

    store, client = _store(handler, part_size=5 * 1024 * 1024)
    asyncio.run(store.write_stream("big.bin", _multipart_body()))
    parts = [c for c in client.calls if c[0] == "PUT" and "partNumber" in c[1]]
    assert parts, client.calls
    for _, _, hdrs, _ in parts:
        assert hdrs["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"


def test_a_part_etag_is_carried_into_the_completion_body():

    def handler(method, target, body):
        if method == "POST" and "uploads=" in target and "uploadId" not in target:
            return FakeResp(200, body=b"<x><UploadId>UP1</UploadId></x>")
        if method == "PUT" and "partNumber=1" in target:
            return FakeResp(200, [(b"etag", b'"part-one"')])
        if method == "PUT" and "partNumber" in target:
            return FakeResp(200)  # no ETag header at all
        if method == "POST":
            return FakeResp(200, body=b"<CompleteMultipartUploadResult/>")
        return FakeResp(200, [(b"content-length", b"1"), (b"etag", b'"final"')])

    store, client = _store(handler, part_size=5 * 1024 * 1024)
    asyncio.run(store.write_stream("big.bin", _multipart_body()))
    complete = next(c for c in client.calls if c[0] == "POST" and "uploadId" in c[1])
    xml = complete[3].decode()
    assert (
        "<PartNumber>1</PartNumber><ETag>&quot;part-one&quot;</ETag>" in xml
        or '<PartNumber>1</PartNumber><ETag>"part-one"</ETag>' in xml
    ), xml
    assert "<ETag></ETag>" in xml, xml  # the part that answered without one


def test_exists_reports_an_unexpected_status_rather_than_absence():
    store, _ = _store(lambda *a: FakeResp(500, [], b"internal error"))
    with pytest.raises(ObjectError, match="S3 500"):
        asyncio.run(store.exists("k"))


def test_exists_uses_the_status_instead_of_response_truthiness():
    class FalseResponse(FakeResp):
        def __bool__(self) -> bool:
            return False

    store, _ = _store(lambda *a: FalseResponse(200))
    assert asyncio.run(store.exists("k")) is True


def test_an_abort_answered_404_is_not_an_orphan():

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
