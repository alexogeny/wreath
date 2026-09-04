from __future__ import annotations

import os
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from wreath.objects import LocalObjectStore, MemoryObjectStore, ObjectError


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no named pipes")
async def test_a_named_pipe_is_not_a_local_object(tmp_path) -> None:
    pipe = tmp_path / "events"
    os.mkfifo(pipe)
    held = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
    store = LocalObjectStore(tmp_path)
    try:
        with pytest.raises(ObjectError, match="regular file"):
            await store.stat("events")
    finally:
        store.close()
        os.close(held)


@pytest.mark.parametrize("kind", ["local", "memory"])
def test_a_local_grant_percent_encodes_its_object_key(tmp_path, kind: str) -> None:
    store = (
        LocalObjectStore(tmp_path, url_secret=b"s" * 32)
        if kind == "local"
        else MemoryObjectStore(url_secret=b"s" * 32)
    )
    key = "reports/q3?# %£.csv"
    try:
        grant = store.url(key, expires=60)
        parsed = urlsplit(grant)
        granted_key = unquote(parsed.path.lstrip("/"))
        query = parse_qs(parsed.query, strict_parsing=True)

        assert granted_key == key
        assert parsed.fragment == ""
        assert store.verify_local_url(
            granted_key,
            method="GET",
            expires=int(query["expires"][0]),
            signature=query["signature"][0],
        )
    finally:
        if isinstance(store, LocalObjectStore):
            store.close()
