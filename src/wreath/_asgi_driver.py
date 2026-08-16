"""One warm, synchronous owner for an ASGI app and its lifespan."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

ASGI = {"version": "3.0", "spec_version": "2.5"}


def _encode_http_header(
    name: Any,
    value: Any,
    *,
    owner: str,
    error_type: type[Exception],
) -> tuple[bytes, bytes]:
    """Encode one host-platform header into ASGI's byte representation."""
    try:
        return str(name).lower().encode("ascii"), str(value).encode("latin-1")
    except UnicodeEncodeError as exc:
        raise error_type(
            f"{owner} header {name!r} is not HTTP Latin-1"
        ) from exc


@dataclass(frozen=True, slots=True)
class ASGIResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


class WarmASGIDriver:
    """Own one loop and lifespan for synchronous warm-host adapters."""

    __slots__ = (
        "_app",
        "_closed",
        "_from_app",
        "_lifespan_task",
        "_lock",
        "_owner",
        "_runner",
        "_started",
        "_to_app",
    )

    def __init__(self, app: Any, *, owner: str) -> None:
        if not callable(app):
            raise TypeError(f"{owner} app must be an ASGI callable")
        self._app = app
        self._owner = owner
        self._runner = asyncio.Runner()
        self._lock = threading.Lock()
        self._started = False
        self._closed = False
        self._to_app: asyncio.Queue[dict[str, Any]] | None = None
        self._from_app: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan_task: asyncio.Task[None] | None = None

    def invoke(self, scope: dict[str, Any], body: bytes) -> ASGIResponse:
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    f"{self._owner} is closed; construct a new adapter"
                )
            if not self._started:
                try:
                    self._runner.run(self._startup())
                except BaseException:
                    self._runner.close()
                    self._closed = True
                    raise
                self._started = True
            return self._runner.run(self._invoke(scope, body))

    async def _startup(self) -> None:
        self._to_app = asyncio.Queue()
        self._from_app = asyncio.Queue()

        async def receive() -> dict[str, Any]:
            if self._to_app is None:
                raise RuntimeError("ASGI lifespan receive queue is not initialized")
            return await self._to_app.get()

        async def send(message: dict[str, Any]) -> None:
            if self._from_app is None:
                raise RuntimeError("ASGI lifespan send queue is not initialized")
            await self._from_app.put(message)

        self._lifespan_task = asyncio.create_task(
            self._app({"type": "lifespan", "asgi": ASGI}, receive, send),
            name="wreath-warm-asgi-lifespan",
        )
        self._to_app.put_nowait({"type": "lifespan.startup"})
        reply = await self._lifespan_reply("startup")
        if reply is None:
            return
        if reply.get("type") == "lifespan.startup.failed":
            raise RuntimeError(
                f"{self._owner} ASGI lifespan startup failed: "
                f"{reply.get('message', '')}"
            )
        if reply.get("type") != "lifespan.startup.complete":
            raise RuntimeError(
                f"ASGI app sent {reply.get('type')!r}; expected "
                "lifespan.startup.complete or lifespan.startup.failed"
            )

    async def _lifespan_reply(self, phase: str) -> dict[str, Any] | None:
        if self._from_app is None or self._lifespan_task is None:
            raise RuntimeError("ASGI lifespan is not initialized")
        lifespan_task = self._lifespan_task
        reply = asyncio.create_task(self._from_app.get())
        done, _pending = await asyncio.wait(
            (reply, lifespan_task), return_when=asyncio.FIRST_COMPLETED
        )
        if reply in done:
            return reply.result()
        reply.cancel()
        try:
            await reply
        except asyncio.CancelledError:
            pass
        if phase == "startup":
            # Lifespan 2.0 defines a raise on the initial lifespan scope as the
            # application's opt-out signal. A clean return without a reply is
            # treated the same way: neither shape can accept shutdown later.
            if not lifespan_task.cancelled():
                lifespan_task.exception()
            self._lifespan_task = None
            self._to_app = None
            self._from_app = None
            return None
        if lifespan_task.cancelled():
            raise RuntimeError(
                f"ASGI app was cancelled during {self._owner} lifespan {phase}"
            )
        error = lifespan_task.exception()
        if error is not None:
            raise RuntimeError(
                f"ASGI app raised during {self._owner} lifespan {phase}"
            ) from error
        raise RuntimeError(
            f"ASGI app ended without lifespan.{phase}.complete or .failed"
        )

    async def _invoke(self, scope: dict[str, Any], body: bytes) -> ASGIResponse:
        sent_request = False
        response_finished = False
        status: int | None = None
        headers: tuple[tuple[bytes, bytes], ...] = ()
        chunks: list[bytes] = []
        response_complete = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal sent_request
            if sent_request:
                await response_complete.wait()
                return {"type": "http.disconnect"}
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            nonlocal headers, response_finished, status
            kind = message.get("type")
            if response_finished:
                raise RuntimeError("ASGI app sent a message after the response ended")
            if kind == "http.response.start":
                if status is not None:
                    raise RuntimeError("ASGI app sent two response starts")
                status = int(message["status"])
                headers = tuple(message.get("headers", ()))
                return
            if kind == "http.response.body":
                if status is None:
                    raise RuntimeError("ASGI app sent a body before response start")
                chunks.append(bytes(message.get("body", b"")))
                response_finished = not bool(message.get("more_body", False))
                if response_finished:
                    response_complete.set()
                return
            raise RuntimeError(f"ASGI app sent unsupported HTTP message {kind!r}")

        await self._app(scope, receive, send)
        if status is None:
            raise RuntimeError("ASGI app returned without starting a response")
        if not response_finished:
            raise RuntimeError("ASGI app returned before ending the response body")
        return ASGIResponse(status, headers, b"".join(chunks))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._started and self._lifespan_task is not None:
                    self._runner.run(self._shutdown())
            finally:
                self._runner.close()
                self._closed = True

    async def _shutdown(self) -> None:
        if self._to_app is None:
            raise RuntimeError("ASGI lifespan receive queue is not initialized")
        self._to_app.put_nowait({"type": "lifespan.shutdown"})
        reply = await self._lifespan_reply("shutdown")
        if reply is None:
            raise RuntimeError("ASGI lifespan ended before shutdown completed")
        if self._lifespan_task is not None:
            await self._lifespan_task
        if reply.get("type") == "lifespan.shutdown.failed":
            raise RuntimeError(
                f"{self._owner} ASGI lifespan shutdown failed: "
                f"{reply.get('message', '')}"
            )
        if reply.get("type") != "lifespan.shutdown.complete":
            raise RuntimeError(
                f"ASGI app sent {reply.get('type')!r}; expected "
                "lifespan.shutdown.complete or lifespan.shutdown.failed"
            )
