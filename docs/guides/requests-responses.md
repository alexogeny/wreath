---
keywords: query string, request body, json body, response headers, status code, streaming response, file response, redirect, querystring
---
# Requests and responses

Every handler sits between two things: the `Request` that came in, and the
response you send back. Wreath tries to make the common case effortless and the
uncommon case fully within reach.

## User story: a created record and a redirect

> *As an API author, most of my handlers just return a dict and I want that to
> Just Work as JSON. But my create endpoint should answer `201 Created`, not the
> default `200`, and one legacy path has to redirect. I don't want to drop to raw
> ASGI for either.*

```python
from wreath.response import RedirectResponse

@app.post("/users", status_code=201)
async def create_user(request) -> dict:
    user = await save(await request.json())
    return {"id": user.id}

@app.get("/old-path")
async def moved(request) -> RedirectResponse:
    return RedirectResponse("/new-path", status=308)
```

Return a plain dict for the ordinary case and Wreath sends JSON. `status_code=`
on the route changes the status for a coerced value and the OpenAPI response
together; reach for a response type when one call needs to own the status,
headers, redirect, stream, or file itself.

`RequestLimits.max_cookie_bytes` (16 KiB) bounds the `Cookie` header before it is
parsed — parsing builds a dict proportional to whatever arrived, on every route
that reads a session or a CSRF token. Past it, `431`. The header *count* is
deliberately left to the server in front: it is already bounded by every server's
frame limits, and a second check here cost a boundary crossing in
`pre_activation`.


## The request

The `Request` carries everything about the incoming call — method, URL, headers,
cookies, the raw body, parsed JSON, and form data:

```python
from wreath import Request

@app.post("/echo")
async def echo(request: Request) -> dict:
    data = await request.json()
    user_agent = request.header("user-agent")
    return {"you_sent": data, "user_agent": user_agent}
```

Reading the request by hand like this is always available, but for anything you
expect and depend on — path and query parameters, a typed body — prefer
[declaring it](binding.md). Declared inputs are validated and documented for you,
and your handler receives clean, typed values instead of raw strings.

For a body that must not be retained, iterate the receive channel directly:

```python
@app.post("/events")
async def ingest(request: Request) -> dict:
    async for chunk in request.stream():
        await sink.write(chunk)
    return {"accepted": True}
```

The stream is one-shot and still enforces `RequestLimits.max_body_bytes` while
chunks arrive. Calling `body()`, `json()`, or `form()` after it raises
`StreamConsumed`. Calling `stream()` after `body()` replays the cached body.
Multipart forms use this incremental path themselves, so a large uploaded file
can cross the spool threshold without a second whole-request buffer.

## Responses

A supported return annotation is also the runtime output contract. A dataclass
response filters undeclared mapping keys, validates the remaining values, and
serializes aliases and rich scalar types before JSON encoding. Returning a
`Response` object bypasses that projection because the response owns its wire
representation explicitly.

Return a `dict` and Wreath sends JSON. When you need to shape the response
yourself — a specific status, custom headers, a stream, a file — reach for the
response type that says what you mean:

```python
from wreath.response import (
    JSONResponse, TextResponse, HTMLResponse,
    RedirectResponse, StreamingResponse, FileResponse, ProblemResponse,
)

@app.get("/text")
async def text(request) -> TextResponse:
    return TextResponse("hello")

@app.get("/download")
async def download(request) -> FileResponse:
    return FileResponse("report.pdf")

@app.get("/stream")
async def stream(request) -> StreamingResponse:
    async def chunks():
        for chunk in (b"a", b"b", b"c"):
            yield chunk
    return StreamingResponse(chunks())
```

For a route with route-scoped middleware whose handler always returns one of
these response objects directly, `response_only=True` records that promise at
startup and removes the middleware chain's response-coercion wrapper:

```python
@app.get("/health", middleware=(audit,), response_only=True)
async def health(request) -> TextResponse:
    return TextResponse("ok")
```

Do not set it on a handler that may return a dict, string, or bytes. The normal
route path deliberately keeps coercion so those convenient return values retain
their documented meaning.

Errors are responses too. Raise an [`HTTPException`](../reference/exceptions.md)
subclass when something goes wrong and Wreath turns it into a proper response;
for machine-readable errors, return a `ProblemResponse`, which follows RFC 9457
(`application/problem+json`).

**Reference:** [`wreath.request`](../reference/request.md),
[`wreath.response`](../reference/response.md).
