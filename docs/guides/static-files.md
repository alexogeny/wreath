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

**Reference:** [`wreath.staticfiles`](../reference/staticfiles.md).
