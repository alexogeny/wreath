"""Response-bound background task behaviour.

Covers the task primitives themselves (classification, argument binding,
thread-offload) and their lifecycle contract through the ASGI application:
tasks run after the complete response is emitted, in order, and never after a
failed emission.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from pathlib import Path
from typing import Any

import pytest

from wreath import JSONResponse, Response, Wreath
from wreath.background import BackgroundTask, BackgroundTasks
from wreath.response import FileResponse, StreamingResponse

pytestmark = pytest.mark.asyncio


async def invoke(
    app: Wreath,
    path: str = "/",
    *,
    method: str = "GET",
    extensions: dict[str, Any] | None = None,
    record: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Drive one HTTP request. When ``record`` is given, each sent message is
    appended to it so a background task sharing the list can be ordered against
    the response body."""
    messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if record is not None:
            record.append(("send", message["type"]))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    if extensions is not None:
        scope["extensions"] = extensions
    await app(scope, receive, send)
    return sent


# --- Task primitives -------------------------------------------------------


async def test_async_callable_receives_arguments() -> None:
    seen: list[Any] = []

    async def work(a: int, *, b: int) -> None:
        seen.append((a, b))

    await BackgroundTask(work, 1, b=2)()
    assert seen == [(1, 2)]


async def test_sync_callable_receives_arguments_off_the_loop_thread() -> None:
    seen: list[Any] = []
    loop_thread = threading.get_ident()

    def work(a: int, *, b: int) -> None:
        seen.append((a, b, threading.get_ident()))

    await BackgroundTask(work, 1, b=2)()
    (a, b, thread), = seen
    assert (a, b) == (1, 2)
    assert thread != loop_thread  # ran via asyncio.to_thread


async def test_classification_of_callable_forms() -> None:
    async def afunc() -> None: ...
    def sfunc() -> None: ...

    class AsyncObj:
        async def __call__(self) -> None: ...

    class Holder:
        async def method(self) -> None: ...

    assert BackgroundTask(afunc)._is_async is True
    assert BackgroundTask(sfunc)._is_async is False
    assert BackgroundTask(functools.partial(afunc))._is_async is True
    assert BackgroundTask(AsyncObj())._is_async is True
    assert BackgroundTask(Holder().method)._is_async is True


async def test_sync_callable_returning_awaitable_is_awaited() -> None:
    seen: list[str] = []

    async def tail() -> None:
        seen.append("tail")

    def factory() -> Any:
        # Classified sync (plain def), but hands back an awaitable to finish.
        seen.append("body")
        return tail()

    await BackgroundTask(factory)()
    assert seen == ["body", "tail"]


async def test_group_runs_in_insertion_order() -> None:
    order: list[int] = []

    async def work(n: int) -> None:
        order.append(n)

    tasks = BackgroundTasks()
    for n in range(4):
        tasks.add_task(work, n)
    await tasks()
    assert order == [0, 1, 2, 3]


async def test_group_stops_on_first_failure() -> None:
    order: list[int] = []

    async def ok(n: int) -> None:
        order.append(n)

    async def boom() -> None:
        raise RuntimeError("boom")

    tasks = BackgroundTasks()
    tasks.add_task(ok, 0)
    tasks.add_task(boom)
    tasks.add_task(ok, 2)
    with pytest.raises(RuntimeError, match="boom"):
        await tasks()
    assert order == [0]  # task after the failure never ran


# --- Lifecycle through the application -------------------------------------


async def test_task_runs_after_final_body_message() -> None:
    record: list[Any] = []
    app = Wreath()

    async def note() -> None:
        record.append(("task", "ran"))

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return JSONResponse({"ok": True}, background=BackgroundTask(note))

    await invoke(app, record=record)
    assert record[-1] == ("task", "ran")
    assert ("send", "http.response.body") in record
    # The task ran strictly after the final body send.
    assert record.index(("task", "ran")) > record.index(("send", "http.response.body"))


async def test_raw_zero_arg_async_callback_still_runs() -> None:
    ran: list[bool] = []
    app = Wreath()

    async def callback() -> None:
        ran.append(True)

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        response = JSONResponse({"ok": True})
        response.background = callback  # legacy direct assignment
        return response

    await invoke(app)
    assert ran == [True]


async def test_group_failure_propagates_from_asgi_invocation() -> None:
    app = Wreath()

    async def boom() -> None:
        raise RuntimeError("post-response failure")

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        tasks = BackgroundTasks()
        tasks.add_task(boom)
        return Response(b"accepted", status=202, background=tasks)

    with pytest.raises(RuntimeError, match="post-response failure"):
        await invoke(app)


async def test_failed_emission_prevents_task_execution() -> None:
    ran: list[bool] = []
    app = Wreath()

    async def note() -> None:
        ran.append(True)

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return Response(b"ok", background=BackgroundTask(note))

    # A send that raises on the body frame simulates a broken transport.
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise ConnectionError("client gone")

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    with pytest.raises(ConnectionError):
        await app(scope, receive, send)
    assert ran == []  # emission failed, so background work never started


async def test_head_runs_task_after_emission() -> None:
    record: list[Any] = []
    app = Wreath()

    async def note() -> None:
        record.append(("task", "ran"))

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return Response(b"hello", background=BackgroundTask(note))

    await invoke(app, method="HEAD", record=record)
    assert record[-1] == ("task", "ran")
    assert record.index(("task", "ran")) > record.index(("send", "http.response.body"))


async def test_native_one_shot_response_runs_task_after_send() -> None:
    record: list[Any] = []
    app = Wreath()

    async def note() -> None:
        record.append(("task", "ran"))

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return Response(b"hello", background=BackgroundTask(note))

    sent = await invoke(app, extensions={"wreath.response": {}}, record=record)
    assert any(m["type"] == "wreath.response" for m in sent)
    assert record[-1] == ("task", "ran")
    assert record.index(("task", "ran")) > record.index(("send", "wreath.response"))


async def test_streaming_response_runs_task_after_terminal_frame() -> None:
    record: list[Any] = []
    app = Wreath()

    async def note() -> None:
        record.append(("task", "ran"))

    async def body() -> Any:
        yield b"chunk-1"
        yield b"chunk-2"

    @app.get("/")
    async def endpoint(request: Any) -> StreamingResponse:
        return StreamingResponse(body(), background=BackgroundTask(note))

    sent = await invoke(app, record=record)
    # Last body frame is the empty terminal frame with more_body False.
    assert sent[-1]["body"] == b"" and sent[-1].get("more_body") is False
    assert record[-1] == ("task", "ran")


async def test_file_response_runs_task_after_emission(tmp_path: Path) -> None:
    record: list[Any] = []
    app = Wreath()
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"file-body")

    async def note() -> None:
        record.append(("task", "ran"))

    @app.get("/")
    async def endpoint(request: Any) -> FileResponse:
        return FileResponse(str(payload), background=BackgroundTask(note))

    sent = await invoke(app, record=record)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == b"file-body"
    assert record[-1] == ("task", "ran")
    assert record.index(("task", "ran")) > record.index(("send", "http.response.body"))


async def test_cancellation_is_not_shielded() -> None:
    app = Wreath()
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(10)  # cancelled while pending

    @app.get("/")
    async def endpoint(request: Any) -> Response:
        return Response(b"ok", background=BackgroundTask(slow))

    task = asyncio.ensure_future(invoke(app))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
