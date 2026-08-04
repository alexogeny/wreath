---
description: Reverse proxy and in-memory load balancer — put wreath in front of your origins without a second stack.
keywords: reverse proxy, load balancer, nginx, haproxy, upstream, x-forwarded-for, ingress
---
# `wreath.edge`

A reverse proxy and load balancer built from the parts wreath already has: its
own server on the way in, its own HTTP client on the way out, and one small
in-memory table deciding where each request goes.

Reach for it when a deployment is small enough that a separate proxy is more
operational surface than it is worth — the same configuration language, the same
logs and the same flight recorder as the applications behind it, rather than a
second stack with a second config format.

Read [Putting wreath at the edge](../guides/edge.md) first; it explains the
choices this module makes and, just as importantly, the ones it has not made
yet.

::: wreath.edge

::: wreath.edge.upstream

::: wreath.edge.headers

::: wreath.edge.proxy
