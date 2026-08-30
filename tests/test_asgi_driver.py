import asyncio

import pytest

from wreath._asgi_driver import WarmASGIDriver


def test_a_warm_driver_refuses_a_non_callable_app_at_construction() -> None:
    with pytest.raises(TypeError, match="lambda app must be an ASGI callable"):
        WarmASGIDriver(object(), owner="lambda")


@pytest.mark.asyncio
async def test_request_is_followed_by_disconnect_only_after_the_response() -> None:
    observed: list[str | bool] = []

    async def app(scope, receive, send) -> None:
        observed.append((await receive())["type"])
        disconnect = asyncio.create_task(receive())
        await asyncio.sleep(0)
        observed.append(disconnect.done())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        observed.append((await disconnect)["type"])

    driver = WarmASGIDriver(app, owner="test")
    response = await asyncio.wait_for(driver._invoke({"type": "http"}, b"body"), timeout=0.5)

    assert response.status == 204
    assert observed == ["http.request", False, "http.disconnect"]
