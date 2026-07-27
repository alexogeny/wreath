# Let a client upload straight to object storage

Large uploads shouldn't flow through your API process at all. Instead, hand the
browser a short-lived URL, let it `PUT` directly to the bucket, and just record
the key. The signature is computed in-process — there is no round trip to S3 to
mint it:

```python
app.objects("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")

@app.post("/uploads")
async def start_upload(request):
    store = app.state.objects_assets
    body = await request.json()
    url = store.url(f"incoming/{body['key']}", expires=900, method="PUT")   # seconds
    return {"upload_url": url}
```

`store.url(...)` signs locally. On S3 it is a fully query-signed URL the client
`PUT`s to directly; on `LocalObjectStore` it is a signed relative path you verify at
your own route with `store.verify_local_url(...)` — so the same handler works in
dev without an S3 bucket. For a download link, pass `method="GET"` instead.

Presign expiry is in seconds and clamped by the provider (SigV4 max is 7 days).
The URL embeds its own signing time, so client clock skew doesn't matter, and no
credentials are ever exposed to the browser.

If you'd rather stream the bytes *through* your process — to validate or
transform them — write to `store.write_stream(...)` from a multipart handler
instead; on S3 that switches to a multipart upload once the data crosses the part
size, so large files never fully materialize.
