from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.testing import TestClient

_BODY = b"0123456789" * 10  # 100 bytes


@pytest.fixture
def served(tmp_path):
    (tmp_path / "data.bin").write_bytes(_BODY)
    app = Wreath()
    app.static("/files", str(tmp_path))
    return app


class TestRangeParsing:
    """The header is a small grammar and every branch of it is a way to serve
    the wrong bytes."""

    def test_a_simple_range(self):
        from wreath.response import parse_range

        assert parse_range("bytes=0-9", 100) == (0, 9)
        assert parse_range("bytes=10-19", 100) == (10, 19)

    def test_an_open_ended_range(self):
        from wreath.response import parse_range

        assert parse_range("bytes=90-", 100) == (90, 99)

    def test_a_suffix_range(self):
        from wreath.response import parse_range

        assert parse_range("bytes=-10", 100) == (90, 99)
        # Longer than the file: the whole file, not an error.
        assert parse_range("bytes=-500", 100) == (0, 99)

    def test_an_end_past_the_file_is_clamped(self):
        from wreath.response import parse_range

        assert parse_range("bytes=95-500", 100) == (95, 99)

    def test_unsatisfiable_ranges(self):
        from wreath.response import UNSATISFIABLE, parse_range

        assert parse_range("bytes=100-", 100) is UNSATISFIABLE
        assert parse_range("bytes=200-300", 100) is UNSATISFIABLE
        assert parse_range("bytes=-0", 100) is UNSATISFIABLE

    def test_forms_that_are_ignored_rather_than_refused(self):
        from wreath.response import parse_range

        assert parse_range("bytes=0-9,20-29", 100) is None  # multi-range
        assert parse_range("items=0-9", 100) is None  # not bytes
        assert parse_range("bytes=abc", 100) is None
        assert parse_range("", 100) is None
        assert parse_range(None, 100) is None
        assert parse_range("bytes=9-0", 100) is None  # start past end


class TestRangeResponses:
    async def test_a_range_returns_206_and_the_slice(self, served):
        async with TestClient(served) as client:
            response = await client.get("/files/data.bin", headers={"range": "bytes=10-19"})
        assert response.status == 206
        assert response.body == _BODY[10:20]
        assert response.header("content-range") == "bytes 10-19/100"
        assert response.header("content-length") == "10"

    async def test_a_suffix_range_returns_the_tail(self, served):
        async with TestClient(served) as client:
            response = await client.get("/files/data.bin", headers={"range": "bytes=-5"})
        assert response.status == 206
        assert response.body == _BODY[-5:]
        assert response.header("content-range") == "bytes 95-99/100"

    async def test_an_unsatisfiable_range_is_416(self, served):
        async with TestClient(served) as client:
            response = await client.get("/files/data.bin", headers={"range": "bytes=500-600"})
        assert response.status == 416
        assert response.header("content-range") == "bytes */100"

    async def test_an_ignored_form_sends_the_whole_file(self, served):
        async with TestClient(served) as client:
            response = await client.get("/files/data.bin", headers={"range": "bytes=0-9,20-29"})
        assert response.status == 200
        assert response.body == _BODY

    async def test_a_plain_request_advertises_the_capability(self, served):
        async with TestClient(served) as client:
            response = await client.get("/files/data.bin")
        assert response.status == 200
        assert response.header("accept-ranges") == "bytes"
        assert response.body == _BODY

    async def test_a_conditional_hit_still_wins(self, served):
        async with TestClient(served) as client:
            first = await client.get("/files/data.bin")
            response = await client.get(
                "/files/data.bin",
                headers={
                    "if-none-match": first.header("etag"),
                    "range": "bytes=0-9",
                },
            )
        assert response.status == 304

    async def test_an_if_range_that_does_not_match_sends_everything(self, served):
        async with TestClient(served) as client:
            response = await client.get(
                "/files/data.bin",
                headers={"if-range": '"stale"', "range": "bytes=0-9"},
            )
        assert response.status == 200
        assert response.body == _BODY

    async def test_a_matching_if_range_serves_the_range(self, served):
        async with TestClient(served) as client:
            first = await client.get("/files/data.bin")
            response = await client.get(
                "/files/data.bin",
                headers={"if-range": first.header("etag"), "range": "bytes=0-9"},
            )
        assert response.status == 206
        assert response.body == _BODY[:10]


class TestFileResponseDirect:
    async def test_a_file_response_can_be_asked_for_a_range(self, tmp_path):
        from wreath.response import FileResponse

        target = tmp_path / "x.bin"
        target.write_bytes(_BODY)

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await FileResponse(target, range=(10, 19))(send)
        assert sent[0]["status"] == 206
        assert b"".join(m.get("body", b"") for m in sent) == _BODY[10:20]

    async def test_a_whole_file_is_unchanged(self, tmp_path):
        from wreath.response import FileResponse

        target = tmp_path / "x.bin"
        target.write_bytes(_BODY)

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await FileResponse(target)(send)
        assert sent[0]["status"] == 200
        assert b"".join(m.get("body", b"") for m in sent) == _BODY
