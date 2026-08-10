"""Repository tests that patch the legacy manager class."""

from unittest.mock import patch

from .models import Camera


async def test_missing_camera():
    with patch.object(Camera.objects.__class__, "get_or_none", return_value=None):
        assert await find_camera("lost") is None
