# Requests and responses

Every handler sits between two things: the `Request` that came in, and the
response you send back. Wreath tries to make the common case effortless and the
uncommon case fully within reach.

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

## Responses

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

Errors are responses too. Raise an [`HTTPException`](../reference/exceptions.md)
subclass when something goes wrong and Wreath turns it into a proper response;
for machine-readable errors, return a `ProblemResponse`, which follows RFC 9457
(`application/problem+json`).

**Reference:** [`wreath.request`](../reference/request.md),
[`wreath.response`](../reference/response.md).
