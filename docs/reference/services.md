# `wreath.services`

The supervised-service `Supervisor` that owns process-lifetime workers and drains them on shutdown.

Register any structural service with `app.service(name, service)`. It must expose
`async start(supervisor)` and `async drain(deadline)`. Generic services start
after databases and HTTP clients, before user startup handlers, and drain before
their dependencies close. This is also the small composition seam for process
roles: one `build(role=...)` factory can register HTTP routers, workers, or both
without creating separate application globals.

::: wreath.services
