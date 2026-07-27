# Static files

`wreath.staticfiles` serves files from a directory — your CSS, your images, a
built frontend bundle — with the details handled for you: conditional requests
(`ETag` / `If-None-Match`) so an unchanged file comes back as a `304`, streamed
bodies for large files, and firm protection against path-traversal attempts
that try to escape the directory you offered.

```python
app.static("/assets", "static/")
```

Static responses travel the same path as everything else, so your global
middleware still applies to them — the security headers and compression you set
up once cover your assets as well as your API.

## User story: cache a fingerprinted asset bundle hard

> *As an API author, I ship a built frontend whose asset filenames are
> content-hashed (`app.9f3a.js`). A file never changes under a given name, so I
> want browsers to cache those hard — a year, `immutable` — instead of
> revalidating them on every visit.*

```python
from wreath.cache_control import CacheControl

app.static(
    "/assets",
    "dist/assets",
    cache_control=CacheControl(public=True, max_age=31536000, immutable=True),
)
```

Because the filenames are fingerprinted, a hard `Cache-Control: public,
max-age=31536000, immutable` is safe — a new build ships new names, so a cached
file can never go stale under the name it was served with. Files you don't set a
policy for still get `ETag` / `If-None-Match` revalidation for free.

**Reference:** [`wreath.staticfiles`](../reference/staticfiles.md).

Conditional requests follow RFC 9110 §13.1.2: `If-None-Match` is parsed as a
list, `*` matches anything, and the weak `W/` prefix is ignored when comparing.
Responses carry `Last-Modified` alongside the `ETag`.

A directory reached without its trailing slash (`/assets/sub`) answers `308` to
`/assets/sub/` rather than serving the index from the wrong base — otherwise
every relative link in that page resolves one level up.


### Ranges

`Range` is honoured: `206` with `Content-Range` for a single byte range, `416`
for one entirely past the end, and `Accept-Ranges: bytes` on every response so
clients know to try. A multi-range request, an unknown unit, or anything
malformed is *ignored* and the whole file is sent, per RFC 9110 §14.2.
`If-Range` is checked first — a stale validator falls back to the whole file
rather than splicing new bytes into an old copy.

Lookups run on the handler's own bounded thread pool rather than the loop's
default executor, so a burst of static requests cannot starve unrelated work.

