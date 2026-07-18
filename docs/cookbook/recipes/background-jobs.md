# Run work after the response

Sometimes the last thing a request does — sending a receipt, warming a cache,
notifying another service — doesn't need to happen before the caller gets their
answer. Making them wait for it is just latency you're choosing to pay. A
response-bound background task runs *after* the response is on its way:

```python
from wreath.background import BackgroundTask
from wreath.response import JSONResponse

@app.post("/orders")
async def create(request) -> JSONResponse:
    order = await place_order(await request.json())
    response = JSONResponse({"id": order.id}, status=201)
    response.background = BackgroundTask(send_receipt, order.id)
    return response
```

The caller gets their `201` immediately; the receipt goes out afterward. Use
`BackgroundTasks` when you have several to run. These tasks are bound to the
response and run once its body has been flushed — they're for finishing touches,
not for long-running jobs, which belong on a real queue.
