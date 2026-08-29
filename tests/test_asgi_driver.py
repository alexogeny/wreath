import pytest

from wreath._asgi_driver import WarmASGIDriver


def test_a_warm_driver_refuses_a_non_callable_app_at_construction() -> None:
    with pytest.raises(TypeError, match="lambda app must be an ASGI callable"):
        WarmASGIDriver(object(), owner="lambda")
