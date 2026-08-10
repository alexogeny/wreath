"""A domain exception wrapper and request-state middleware."""

from starlette.middleware.base import BaseHTTPMiddleware


class StationStateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.station = station_registry.current()
        return await call_next(request)
