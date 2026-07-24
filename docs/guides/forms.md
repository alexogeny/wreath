# Form-model binding

A single form field binds with a `Form()` marker. A whole *model* — the ten-field settings form, the multi-part upload with metadata — used to mean pulling fields out of `request.form()` by hand, or importing a decorator to teach a model how to read a form. Wreath binds the whole model in one marker.

## Bind an entire model from a form

Annotate the parameter with your model and `Form()`:

```python
from dataclasses import dataclass
from typing import Annotated
from wreath.binding import Form

@dataclass
class Booking:
    name: str
    guests: int
    notes: str = ""

@app.post("/bookings")
async def create(request, data: Annotated[Booking, Form()]) -> dict:
    return {"name": data.name, "guests": data.guests}
```

Every field of `Booking` is read from the multipart form and validated by **the same native validation tape that validates a JSON body** — so a missing required field yields a structured `422`, and extra fields are rejected, exactly as they would be for `Annotated[Booking, Body()]`. The only thing that changed is where the values came from.

Single-field markers still work as before, and can carry an `alias` for form keys that aren't valid identifiers:

```python
from typing import Annotated
from wreath.binding import Form

async def handler(request, display: Annotated[str, Form(alias="display-name")]):
    ...
```

Mix a form-bound model with `File` parameters and the uploaded parts arrive alongside the parsed fields — see [Object storage](storage.md#uploads-land-straight-in-storage) for streaming an upload straight into a bucket.

## Coming from FastAPI

FastAPI users reached for an `as_form` helper decorator to bind a model from a form. In wreath that helper disappears — `Annotated[Model, Form()]` is the whole story — and `wreath port` rewrites the pattern automatically.
