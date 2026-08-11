"""Libraries and services that are not framework features: AWS, webhooks, HTTP,
caching, time and the rest wreath either replaces or leaves alone.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED

INTEGRATIONS: dict[str, tuple[str, str, str, str]] = {
    # boto3 is not one verdict. Object storage became a framework feature when
    # `wreath.objects` shipped (design 09), so an S3 client now has a real target
    # and reporting it as "keep the external library" tells a porter to keep a
    # dependency they can delete. Every other AWS service still has none, so the
    # service name is what splits them — read it rather than judging the import.
    "ext.boto3": (
        "external",
        "other",
        UNSUPPORTED,
        "This talks to an AWS service wreath has no equivalent for. Keep boto3 and this code as it is.",
    ),
    "ext.boto3_scheduler": (
        "external",
        "other",
        NEEDS_REVIEW,
        "A one-shot Scheduler/EventBridge delivery becomes jobs.enqueue(..., run_at=..., key=...). Creating a schedule maps directly; updates and deletes must preserve replacement and cancellation semantics before the external schedule is removed.",
    ),
    "ext.boto3_observability": (
        "external",
        "other",
        NEEDS_REVIEW,
        "CloudWatch metric emission becomes Wreath metrics, structured logging, and Flight Recorder. A Logs Insights query is an external reporting integration and should keep its client until that query is deliberately replaced.",
    ),
    "ext.boto3_identity": (
        "external",
        "other",
        NEEDS_REVIEW,
        "Token verification and group-to-role mapping move to app.oidc_provider() plus BearerTokenBackend. Administrative identity-provider calls remain an external adapter and should not be mistaken for request authentication.",
    ),
    "ext.boto3_s3": (
        "external",
        "other",
        NEEDS_REVIEW,
        "S3 has a replacement built in: S3ObjectStore(bucket=..., region=...) from wreath.objects, with put, get, stat, delete and zip_stream, and ObjectPath for keys. Signing works the same way. What changes is the lifecycle -- the store is declared once on the app and closed for you, instead of being built at import time. There is a recipe for presigned URLs.",
    ),
    "webhook.hmac": (
        "webhook",
        "other",
        NEEDS_REVIEW,
        "This checks a webhook signature by hand, and it only compares the digest -- so anyone who captures a valid request can replay it forever. HMACWebhookVerifier.verify() from wreath.webhooks compares the digest safely, checks the timestamp against a replay window, and refuses an envelope it has already seen. Port the secret and the header names; do not port the comparison.",
    ),
    "ext.aiometer": (
        "external",
        "other",
        NEEDS_REVIEW,
        "Rate limiting and retries around outbound calls are built into the HTTP client: app.http_client(rate=..., retries=...). Drop aiometer and tenacity and set them there.",
    ),
    "ext.s3path": (
        "external",
        "other",
        NEEDS_REVIEW,
        "S3Path becomes ObjectPath with an ObjectStore from wreath.objects.",
    ),
    "ext.gql": (
        "external",
        "other",
        UNSUPPORTED,
        "This is a GraphQL *client*. Wreath serves GraphQL but does not consume it, so keep the gql library.",
    ),
}

LOCKS: dict[str, tuple[str, str, str, str]] = {
    "lock.dlock": (
        "advisory_lock",
        "other",
        NEEDS_REVIEW,
        "Advisory locks are built in: db.lock() and db.try_lock() on the database, or session.lock() inside a transaction. Drop sqlalchemy-dlock.",
    ),
}

CACHING: dict[str, tuple[str, str, str, str]] = {
    # -- caching --------------------------------------------------------------
    "cache.store": (
        "cache",
        "other",
        TRANSLATED,
        "TTLCache and LRUCache become BoundedCache(max_entries=..., ttl=...) from wreath.cache: the same bounded cache with the same eviction, counted against the framework's memory budget. If this caches a table that rarely changes, SnapshotCache with refresh_on() fits better -- but that is a change of approach, not a rename.",
    ),
    "cache.decorator": (
        "cache",
        "other",
        NEEDS_REVIEW,
        "@cachetools.cached becomes @cached(ttl=..., invalidate_on=[Model]) from wreath.response_cache. Naming the models is worth doing: a TTL is a guess, but the ORM announces its writes, so the cache can clear the moment the data changes. cache.invalidate_across_workers(bus) extends that to every worker.",
    ),
}

TIME: dict[str, tuple[str, str, str, str]] = {
    # -- time -----------------------------------------------------------------
    #
    # `wreath.temporal` shipped, so arrow stops being a dependency you have to
    # replace with hand-rolled stdlib and becomes a rename. The catalog said "do
    # not wait for it" while it was designed-not-shipped; leaving that in place
    # once it landed would tell porters to write the code wreath now owns.
    "time.arrow": (
        "time",
        "other",
        TRANSLATED,
        "arrow becomes wreath.temporal, one call at a time: arrow.utcnow() and arrow.now() are temporal.now(), arrow.get(s) is temporal.parse(s), and .humanize() is temporal.relative(value). What you get back is a datetime subclass, so it stores, compares and serializes with no conversion -- and it refuses to be timezone-naive, which is the bug arrow's implicit UTC hides.",
    ),
    "time.arrow_other": (
        "time",
        "other",
        NEEDS_REVIEW,
        "This arrow call has no direct replacement. wreath.temporal covers the clock, parsing and relative wording. What it will not do is shift by months or years, because that is not a fixed number of seconds -- so if that is what this does, say which behaviour you meant.",
    ),
}

EXTERNAL: dict[str, tuple[str, str, str, str]] = {
    # -- libraries that are not framework features ----------------------------
    "ext.pandas": (
        "external",
        "other",
        UNSUPPORTED,
        "This is data analysis, not framework code. Keep pandas and leave the module as it is.",
    ),
    "ext.httpx": (
        "external",
        "other",
        NEEDS_REVIEW,
        "Register one managed HTTPClient with app.http_client(...), then expose calls through a ServiceClient (or a generated typed ServiceClient). The app owns connection lifetime, rate limits, retries, origin pinning, and transport adapters; do not reproduce httpx response semantics as a compatibility layer.",
    ),
}
