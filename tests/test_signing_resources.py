from types import SimpleNamespace

import pytest

from wreath import _sigv4, objects


async def test_store_derives_same_scope_key_once(monkeypatch):
    calls = 0
    original = _sigv4.signing_key

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    sent = []

    async def request(method, target, **kwargs):
        sent.append((method, target, kwargs))
        return SimpleNamespace(status=200, body=b"payload")

    monkeypatch.setattr(_sigv4, "signing_key", counted)
    monkeypatch.setattr(objects, "_amz_date", lambda: "20260905T040000Z")
    store = objects.S3ObjectStore(
        SimpleNamespace(request=request),
        bucket="fixture",
        region="us-east-1",
        access_key="fixture-access",
        secret_key="fixture-secret",
    )
    assert await store.read("one") == b"payload"
    assert await store.read("two") == b"payload"
    assert len(sent) == 2
    assert calls == 1


@pytest.mark.parametrize("operation", ["sign", "presign"])
def test_cached_signatures_match_uncached_through_scope_changes(operation):
    cache = _sigv4._SigningKeyCache()
    options = dict(
        method="GET",
        host="fixture.example",
        path="/unicode/é space",
        region="us-east-1",
        service="s3",
        access_key="fixture-access",
        secret_key="fixture-secret",
        amz_date="20260905T040000Z",
        session_token="fixture-token",
    )
    if operation == "presign":
        options["expires"] = 60
    call = getattr(_sigv4, operation)
    changes = [
        {},
        {"amz_date": "20260905T230000Z"},
        {"amz_date": "20260906T000000Z"},
        {"amz_date": "20260905T040000Z"},
        {"secret_key": "rotated-secret"},
        {"region": "eu-west-1"},
        {"service": "other-service"},
        {"access_key": "rotated-access"},
        {"session_token": "rotated-token"},
    ]
    for change in changes:
        options.update(change)
        expected = call(**options)
        assert call(**options, _key_cache=cache) == expected
        assert call(**options, _key_cache=cache) == expected
        assert len(cache._entry) == 2
        assert len(cache._entry[0]) == 4


async def test_store_shares_key_across_requests_ranges_urls_and_invalidates(monkeypatch):
    sent = []

    async def request(method, target, **kwargs):
        sent.append(dict(kwargs["headers"]))
        return SimpleNamespace(status=200, body=b"payload")

    date = "20260905T040000Z"
    monkeypatch.setattr(objects, "_amz_date", lambda: date)
    store = objects.S3ObjectStore(
        SimpleNamespace(request=request),
        bucket="fixture",
        region="us-east-1",
        access_key="fixture-access",
        secret_key="fixture-secret",
    )
    for field, value in [
        ("_sk", "fixture-secret"),
        ("_sk", "rotated-secret"),
        ("_region", "eu-west-1"),
        ("_service", "other-service"),
        ("_ak", "rotated-access"),
        ("_token", "rotated-token"),
    ]:
        setattr(store, field, value)
        for date in ("20260906T000000Z", "20260905T040000Z"):
            options = dict(
                method="GET",
                host=store._host,
                path="/key",
                region=store._region,
                service=store._service,
                access_key=store._ak,
                secret_key=store._sk,
                amz_date=date,
                session_token=store._token,
            )
            assert await store.read("key") == b"payload"
            expected = _sigv4.sign(**options)
            assert sent[-1][b"Authorization"] == expected["Authorization"].encode()
            entry = store._signing_keys._entry
            await store._ranged("GET", "/key", 0, 6)
            expected = _sigv4.sign(**options, headers={"range": "bytes=0-6"})
            assert sent[-1][b"Authorization"] == expected["Authorization"].encode()
            assert store.url("key", expires=60) == _sigv4.presign(**options, expires=60)
            assert store._signing_keys._entry is entry


def test_caches_are_independent_and_failed_derivation_keeps_previous_entry(monkeypatch):
    first, second = _sigv4._SigningKeyCache(), _sigv4._SigningKeyCache()
    scope = ("fixture-secret", "20260905", "us-east-1", "s3")
    key = first.get(*scope)
    assert second._entry is None
    assert second.get("different-secret", *scope[1:]) != key
    original_entry = first._entry

    def fail(*args):
        raise ValueError("synthetic derivation refusal")

    monkeypatch.setattr(_sigv4, "signing_key", fail)
    assert first.get(*scope) == key
    with pytest.raises(ValueError, match="synthetic derivation refusal"):
        first.get("changed-secret", *scope[1:])
    assert first._entry is original_entry
    assert "fixture-secret" not in repr(first)


def test_interleaved_stores_keep_independent_keys(monkeypatch):
    monkeypatch.setattr(objects, "_amz_date", lambda: "20260905T040000Z")
    stores = [
        objects.S3ObjectStore(
            None,
            bucket="fixture",
            region="us-east-1",
            access_key="fixture-access",
            secret_key=f"fixture-secret-{index}",
        )
        for index in range(2)
    ]
    urls = [store.url("key") for store in stores]
    entries = [store._signing_keys._entry for store in stores]
    assert urls[0] != urls[1]
    for _ in range(3):
        for store, url, entry in zip(stores, urls, entries, strict=True):
            assert store.url("key") == url
            assert store._signing_keys._entry is entry


def test_reentrant_derivation_returns_its_own_scope_key(monkeypatch):
    cache = _sigv4._SigningKeyCache()
    original = _sigv4.signing_key
    outer = ("fixture-outer", "20260905", "us-east-1", "s3")
    inner = ("fixture-inner", "20260906", "eu-west-1", "s3")

    def derive(*scope):
        if scope == outer:
            assert cache.get(*inner) == original(*inner)
        return original(*scope)

    monkeypatch.setattr(_sigv4, "signing_key", derive)
    assert cache.get(*outer) == original(*outer)
    assert cache.get(*inner) == original(*inner)


@pytest.mark.parametrize(
    "operation, extra",
    [
        ("sign", {"host": "bad host"}),
        ("sign", {"headers": {"x-test": "bad\rvalue"}}),
        ("presign", {"expires": 0}),
        ("presign", {"expires": 60, "extra_params": [("X-Amz-Date", "bad")]}),
    ],
)
def test_cached_signing_preserves_refusals_before_derivation(operation, extra):
    options = dict(
        method="GET",
        host="fixture.example",
        path="/key",
        region="us-east-1",
        service="s3",
        access_key="fixture-access",
        secret_key="fixture-secret",
        amz_date="20260905T040000Z",
    )
    options.update(extra)
    call = getattr(_sigv4, operation)
    cache = _sigv4._SigningKeyCache()
    with pytest.raises(ValueError) as uncached:
        call(**options)
    with pytest.raises(ValueError) as cached:
        call(**options, _key_cache=cache)
    assert cached.value.args == uncached.value.args
    assert cache._entry is None


def test_cached_presign_matches_published_s3_vector():
    cache = _sigv4._SigningKeyCache()
    for _ in range(2):
        url = _sigv4.presign(
            method="GET",
            host="examplebucket.s3.amazonaws.com",
            path="/test.txt",
            region="us-east-1",
            service="s3",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            amz_date="20130524T000000Z",
            expires=86400,
            _key_cache=cache,
        )
        assert url.endswith(
            "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
        )
