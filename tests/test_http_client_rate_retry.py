"""Outbound HTTPClient rate-limiting (native TokenBucket) + retry backoff with
jitter and Retry-After honouring."""
from __future__ import annotations

import pytest

from wreath.http_client import (
    HTTPClient,
    RatePolicy,
    RetryPolicy,
    _parse_retry_after,
)


def _client(**kw) -> HTTPClient:
    return HTTPClient("t", base_url="https://example.test", **kw)


class _Resp:
    def __init__(self, retry_after: bytes | None = None) -> None:
        self._ra = retry_after

    def header(self, name: bytes) -> bytes | None:
        return self._ra if name == b"retry-after" else None


# -- policy validation -------------------------------------------------------
def test_retry_policy_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_base=0)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_cap=-1)


def test_rate_policy_requires_positive_when_enabled() -> None:
    with pytest.raises(ValueError):
        RatePolicy(enabled=True, capacity=0, rate=1)
    with pytest.raises(ValueError):
        RatePolicy(enabled=True, capacity=1, rate=0)
    RatePolicy()  # disabled default is fine
    RatePolicy(enabled=True, capacity=10, rate=5)


# -- backoff math ------------------------------------------------------------
def test_retry_delay_exponential_without_jitter() -> None:
    client = _client(
        retry=RetryPolicy(attempts=6, jitter=False, backoff_base=0.05, backoff_cap=1.0)
    )
    assert client._retry_delay(0, None) == pytest.approx(0.05)
    assert client._retry_delay(4, None) == pytest.approx(0.8)
    assert client._retry_delay(10, None) == pytest.approx(1.0)  # capped


def test_retry_delay_jitter_within_half_to_full() -> None:
    client = _client(retry=RetryPolicy(jitter=True, backoff_base=0.05, backoff_cap=1.0))
    for _ in range(50):
        d = client._retry_delay(0, None)
        assert 0.025 <= d <= 0.05


def test_retry_after_honoured_and_clamped() -> None:
    client = _client(retry=RetryPolicy(jitter=False, backoff_cap=1.0))
    assert client._retry_delay(0, _Resp(b"2")) == pytest.approx(2.0)
    # An absurd server value is clamped to cap*16 so one header can't hang us.
    assert client._retry_delay(0, _Resp(b"999999")) == pytest.approx(16.0)


def test_parse_retry_after() -> None:
    assert _parse_retry_after(b"5") == 5.0
    assert _parse_retry_after(b"0") == 0.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after(b"-3") is None
    assert _parse_retry_after(b"garbage") is None
    # HTTP-date form (RFC 9110 10.2.3): a far-future date is a large positive delay,
    # a past date clamps to 0, and a malformed date is ignored.
    assert _parse_retry_after(b"Wed, 21 Oct 2099 07:28:00 GMT") > 1_000_000
    assert _parse_retry_after(b"Wed, 21 Oct 1999 07:28:00 GMT") == 0.0
    assert _parse_retry_after(b"Wed, 99 Xxx 2099") is None


# -- throttle path (native TokenBucket) --------------------------------------
async def test_throttle_disabled_is_noop() -> None:
    await _client()._throttle()  # rate disabled → returns immediately


async def test_throttle_admits_first_request() -> None:
    client = _client(rate=RatePolicy(enabled=True, capacity=5, rate=5))
    await client._throttle()  # first token available, no wait/raise
    assert client._rate_bucket is not None


def test_app_http_client_forwards_rate_and_retry() -> None:
    # The app factory (app.http_client) must forward rate=/retry= to HTTPClient
    # so a client registered via the app throttles/retries as configured.
    from wreath import Wreath

    app = Wreath()
    rate = RatePolicy(enabled=True, capacity=5, rate=100.0)
    retry = RetryPolicy(attempts=4)
    client = app.http_client("api", base_url="https://example.test", rate=rate, retry=retry)
    assert client._rate is rate
    assert client._retry is retry
    assert client._rate_bucket is not None  # bucket built => throttling active
