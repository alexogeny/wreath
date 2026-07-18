# Static files

`wreath.staticfiles` serves files from a directory — your CSS, your images, a
built frontend bundle — with the details handled for you: correct caching
headers, range requests for large files, and firm protection against
path-traversal attempts that try to escape the directory you offered.

```python
app.static("/assets", "static/")
```

Static responses travel the same path as everything else, so your global
middleware still applies to them — the security headers and compression you set
up once cover your assets as well as your API.

**Reference:** [`wreath.staticfiles`](../reference/staticfiles.md).
