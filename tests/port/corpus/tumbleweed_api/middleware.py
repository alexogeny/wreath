"""Custom BaseHTTPMiddleware subclass (no direct wreath equivalent base)."""
from starlette.middleware.base import BaseHTTPMiddleware


class TrailStateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.trace_id = request.headers.get("x-trace-id", "-")
        response = await call_next(request)
        response.headers["x-trace-id"] = request.state.trace_id
        return response
