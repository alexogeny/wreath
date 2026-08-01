---
keywords: query parameters, query string, path parameters, url parameters, headers, cookies, form data, file uploads, request body, validation, dependency injection, querystring, url params, get parameters
---
# Binding, validation, and dependencies

Binding is where a raw HTTP request becomes the clean, typed arguments your
handler actually wants — and, fittingly for a framework named Wreath, it is the
place where the request is woven into your function. You declare what you expect
and where it comes from; Wreath compiles a validator for it when the application
starts, so validation costs almost nothing at request time. Anything that
doesn't fit becomes a structured `422` before your handler runs.

```python
from typing import Annotated

from wreath import Request
from wreath.binding import Body, Cookie, Depends, Field, File, Form, Header, Path, Query

@app.get("/items/{id}")
async def show(
    request: Request,
    id: int,
    limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
    trace: Annotated[str | None, Header(alias="x-trace-id")] = None,
) -> dict:
    return {"id": id, "limit": limit, "trace": trace}
```

Each marker names exactly where the value is read from — the path, the query
string, a header, a cookie, the body, a form field, an uploaded file. There is
no cleverness to memorize: `Query` reads the query string, `Header` reads a
header. Markers ride inside `Annotated`, and a default stays an ordinary Python
default on the parameter — the signature never stops being plain Python. Most
of the time you need no marker at all: a name matching a path placeholder is a
path parameter, a parameter annotated with a dataclass (or an ORM model) is the
JSON body, and remaining scalar parameters read from the query string. A body
validated against an [ORM model](orm.md) is checked by that model's own
columns, so the same definition guards your database and your API.

`Query` also carries numeric bounds — `minimum`, `maximum`, and an `overflow`
of `"error"` (a structured `422`) or `"clamp"` (pin to the nearest bound, the
right answer for pagination). Query, header, and cookie values are scalars:
`str`, `int`, `float`, `bool`, or optional unions of them. Anything more
structured belongs in the body.

Dataclass bodies understand UUID, Decimal, Enum, Literal, aware instants, dates,
base64 bytes, fixed and variadic tuples, sets, mappings, unions, and nested
dataclasses. `Field` carries the contract shared by input validation, response
filtering, OpenAPI, and typed clients:

```python
@dataclass
class NewItem:
    display_name: Annotated[
        str,
        Field(
            alias="displayName",
            min_length=3,
            max_length=80,
            pattern=r"^[A-Z]",
            description="Public item name",
            examples=("Wreath",),
        ),
    ]
    rating: Annotated[int, Field(ge=1, le=5)]
```

Unknown fields are rejected on input. On output, a dataclass return annotation
projects a mapping onto its declared wire fields, so an accidental internal key
cannot escape, then validates and serializes it. A violation is a server defect
and answers 500, never a caller-facing 422.

## A protobuf body

A body annotated with a [`@message`](protobuf.md) class binds from protobuf
bytes when the request says `Content-Type: application/x-protobuf` (or
`application/protobuf`, the IANA spelling — Wreath emits the first and reads
both):

```python
from wreath.protobuf import field, message

@message
class Sighting:
    species: str = field(1)
    count: int = field(2)

@app.post("/sightings")
async def record(request, sighting: Sighting) -> dict:
    return {"species": sighting.species}
```

**The annotation is content-negotiated, not protobuf-only.** The same handler
still binds a JSON body, because that is what the `Content-Type` asked for. A
`@message` class is an ordinary dataclass and bound from JSON before protobuf
reached the boundary at all, so reading the annotation as "protobuf only" would
have silently broken every handler that already had one; it also keeps the
request half symmetric with the response half, where
`serialize(request, msg, serializers=(PROTOBUF, JSON))` has always negotiated.
It is what the format's own users need, too: OTLP/HTTP defines a protobuf *and*
a JSON encoding of the same messages behind one path.

### The same shape has two strictnesses, and that is deliberate

Read the body as JSON and an unknown field is **rejected**. Read the same body
as protobuf and an unknown field number is **kept**. One declared shape, two
answers, chosen by `Content-Type` — so it is worth being explicit about why,
rather than leaving it to be discovered:

| Sent as | An unexpected member is | Because |
| --- | --- | --- |
| JSON | a `422` naming it | an unexpected *name* is a typo, and the sender had no way to mean it |
| protobuf | preserved on the message | an unexpected *number* is a peer built against a newer `.proto` |

The second is not leniency, it is the mechanism protobuf exists to provide. The
bytes are kept on the decoded message and `encode` puts them back, so a service
that reads a message, edits one field and forwards it does not strip what a
newer peer sent through it — and a schema rollout does not have to be a
synchronised deploy of every consumer. Refusing would give up the one property
field numbers are for.

Three refusals, three different sentences, because the remedies differ: bytes
that are not readable protobuf (`400`, naming protobuf), a body annotated with a
plain dataclass rather than a `@message` (`400`, naming the class — protobuf
carries numbers, and there is nothing to read the wire against), and an
unreadable JSON body (`400`, naming JSON).

Note that the protobuf path is the codec's validation, not the binding tape's:
the wire types *are* the contract, so `Field(...)` constraints declared for the
JSON path do not run over protobuf bytes. Validate those in the handler when the
same shape is served both ways.

## User story: a search endpoint with safe bounds

> *As an API author, my `/search` endpoint takes a required `q`, an optional
> `limit` I never want above 100, and an optional category filter. I want bad
> input rejected with a clear `422` before my code runs — and I don't want to
> write the parsing or the bounds check by hand.*

```python
from typing import Annotated
from wreath.binding import Query

@app.get("/search")
async def search(
    request,
    q: str,
    limit: Annotated[int, Query(minimum=1, maximum=100, overflow="clamp")] = 20,
    category: str | None = None,
) -> dict:
    return await run_search(q, limit=limit, category=category)
```

`q` has no default, so its absence is a `422`; `limit` is pinned into `[1, 100]`
rather than trusted; `category` is an optional query scalar. Your handler only
ever sees clean, typed values — the raw query string never makes it past the
door.

## Dependencies

Handlers often need the same prepared value — the current user, a database
handle, a parsed pagination window. `Depends` resolves such a value once per
request and hands it to every function that asks for it:

```python
async def current_user(request: Request):
    ...

@app.get("/me")
async def me(request: Request, user = Depends(current_user)) -> dict:
    return {"user": user.id}
```

Binding, validation, and dependency resolution are deliberately one surface. We
did not split them into a container, an injector, and a resolver you would have
to wire together — that is complexity for its own sake. It is all `wreath.binding`,
and it all runs on the same compiled path.

**Reference:** [`wreath.binding`](../reference/binding.md).
