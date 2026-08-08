# `wreath.policy`

First-class HTTP policy is configured once on `Wreath`. Wreath's server compiles
the fixed policy into native ingress and egress; a conforming external ASGI
server uses the readable reference executor. These controls are not middleware
and cannot be installed with `add_middleware()`.

::: wreath.policy

::: wreath.policy.cors

::: wreath.policy.csrf

::: wreath.policy.proxy

::: wreath.policy.ratelimit

::: wreath.policy.request_id

::: wreath.policy.security

::: wreath.policy.timing
