# `wreath.response_cache`

Cache a handler's response in a bounded in-process store, and invalidate it from
the ORM writes behind it rather than from a TTL guess. `Tags` and `CDNPurge`
carry the same declaration one hop further out, so a CDN in front of the
application drops what it holds from the same signal.

Reach for `@cached` when a handler is expensive and its answer is shared; add
`tags=` and a `CDNPurge` when an edge cache is in front of it. The guides are
[Response caching](../guides/response-cache.md) and
[Cache tags and edge purge](../guides/cache-tags.md).

::: wreath.response_cache
