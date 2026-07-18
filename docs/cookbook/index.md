# Cookbook

The guides explain how each part of Wreath works. This is the other half: short,
practical recipes for getting a specific thing done, with the whole solution in
one place so you can read it, adapt it, and move on.

There are two circles of readers here, and each gets its own set.

- **[For developers](#for-developers)** — you're building something and need the
  pattern for a common task.
- **[For coding agents](agents/index.md)** — you're changing this codebase, and
  you need to know the gates it must pass, the invariants it must keep, and how
  to prove a change actually works.

## For developers

- [Deploy behind a proxy or load balancer](recipes/behind-a-proxy.md)
- [Rate-limit an endpoint](recipes/rate-limiting.md)
- [Handle file uploads](recipes/file-uploads.md)
- [Run work after the response](recipes/background-jobs.md)
- [Manage a database pool with the lifespan](recipes/database-lifespan.md)
