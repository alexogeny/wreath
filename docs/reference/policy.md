# `wreath.policy`

First-class HTTP policy is configured once on `Wreath`. Wreath's server compiles
the fixed policy into native ingress and egress; a conforming external ASGI
server uses the readable reference executor. These controls are not middleware
and cannot be installed with `add_middleware()`.

::: wreath.policy

::: wreath.policy.cors

::: wreath.policy.cache

::: wreath.policy.admission

::: wreath.policy.compression

::: wreath.policy.csrf

::: wreath.policy.deadline

::: wreath.policy.maintenance

::: wreath.policy.proxy

::: wreath.policy.idempotency

::: wreath.policy.ratelimit

::: wreath.policy.request_id

::: wreath.policy.request_decompression

::: wreath.policy.security

::: wreath.policy.sessions

::: wreath.policy.signed_routes

::: wreath.policy.timing

::: wreath.policy.traffic
