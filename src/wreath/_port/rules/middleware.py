"""Middleware and lifespan: `add_middleware`, custom middleware classes, and the
startup/shutdown shapes.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

MIDDLEWARE: dict[str, tuple[str, str, str, str]] = {
    "mw.cors": (
        "middleware",
        "other",
        TRANSLATED,
        "add_middleware(CORSMiddleware, ...) -> configure_http_policy(HttpPolicy(cors=CorsPolicy(...)))",
    ),
    "mw.trustedhost": (
        "middleware",
        "other",
        TRANSLATED,
        "TrustedHostMiddleware -> first-class TrustedHostPolicy",
    ),
    "mw.trustedhost_noop": (
        "middleware",
        "other",
        TRANSLATED,
        "TrustedHostMiddleware with allowed_hosts=['*'] accepts every valid host, so remove it instead of emitting a no-op policy.",
    ),
    "mw.state": (
        "middleware",
        "other",
        NEEDS_REVIEW,
        "This middleware writes request or application state. Move startup-owned values into the app factory and request-owned values into an explicit dependency; use structured logging and Flight Recorder for observations rather than a wrapping middleware.",
    ),
    "mw.exception": (
        "middleware",
        "other",
        NEEDS_REVIEW,
        "This middleware translates exceptions. Register the exception type with @app.exception_handler and return the corresponding Wreath response; no request wrapper is needed.",
    ),
    "mw.custom": (
        "middleware",
        "other",
        NEEDS_REVIEW,
        "This is a custom BaseHTTPMiddleware. Check wreath's built-in middleware first, since much of what apps write by hand is already there. If it is genuinely yours, rework it onto wreath's middleware base -- the shape is different: wreath fuses the whole chain at startup instead of nesting one call per layer.",
    ),
}

LIFESPAN: dict[str, tuple[str, str, str, str]] = {
    # The split at `yield` is determined only when it really is a split: a bare
    # `yield` at the top of the body partitions the statements in two, and each
    # half becomes a hook. It stops being a partition when a name made before
    # the yield is used after it (the halves are separate functions, so that name
    # needs a home), when the yield hands a value to the framework, or when it
    # sits inside a `try`/`async with` whose exit is the shutdown.
    "lifespan.ctx": (
        "lifespan",
        "other",
        NEEDS_REVIEW,
        "Startup and shutdown become two functions, @app.on_startup and @app.on_shutdown. This body does not split cleanly at the yield, so the division is yours to make -- the note in brackets says what is in the way.",
    ),
    "lifespan.split": (
        "lifespan",
        "other",
        TRANSLATED,
        "This body splits cleanly at the yield: everything before it becomes an @app.on_startup function and everything after it an @app.on_shutdown one, in the same order. Each takes the app.",
    ),
}
