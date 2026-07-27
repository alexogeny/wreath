# `wreath.middleware`

The middleware protocol plus built-ins: CORS, security headers, compression, rate limiting, request IDs, server timing, proxy headers, CSRF, sessions, idempotency, and trusted hosts.

Every name below is re-exported from `wreath.middleware`, so `from wreath.middleware import CORSMiddleware` works regardless of which submodule defines it. The sections are grouped by submodule because that is where the implementations — and their docstrings — live.

::: wreath.middleware

::: wreath.middleware.base

::: wreath.middleware.cache

::: wreath.middleware.compression

::: wreath.middleware.cors

::: wreath.middleware.csrf

::: wreath.middleware.idempotency

::: wreath.middleware.proxy

::: wreath.middleware.ratelimit

::: wreath.middleware.request_id

::: wreath.middleware.security

::: wreath.middleware.sessions

::: wreath.middleware.timing
