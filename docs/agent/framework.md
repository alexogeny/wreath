# Framework and source guidance

Read this for framework APIs, declarations, binding, typing, exception handling, or public examples.

## Engineering and source rules



- **Refuse at the earliest point the error is knowable, naming the offending
  element *and the correct form*.** Prefer declaration or startup time over
  request time: route compilation runs during the ASGI lifespan scope, so a
  refusal there is a startup failure under any server rather than a per-request
  surprise. Half-supported is worse than refused, and it is *more* code.
- **A broad `except` is the exception, not the rule.** Reach for them in this
  order, and only fall to the next when the one above genuinely cannot work:

  1. **Guard the precondition** so nothing can raise, and the `try` disappears.
     This is also the fast path, from first principles rather than measurement:
     raising and unwinding costs far more than a predicate, so a broad catch on
     a path where the "exceptional" case is *not* rare has routed the common
     case through the expensive machinery. If you are catching something that
     happens often, you wanted a check.
  2. **Catch the specific type.** `except (OSError, ValueError)` is not a blanket
     catch. Name what can actually raise there; a `suppress(Exception)` around a
     database call is hiding driver errors and programming errors alike, and only
     one of those deserves to be survivable.
  3. **Catch broadly, count it, and waive it in place with a written reason.**
     A bare `# noqa: BLE001` is itself a finding, exactly as for the native lints.

  Never swallow `CancelledError`, `KeyboardInterrupt` or `SystemExit`.

  `messaging.MessageBus` is the reference for step 3: the catch is narrow, the
  degradation is counted, and infrastructure failure stays distinguishable from
  a user callback raising (`doorbell_reconnects` versus `handler_errors`). A
  suppression with no counter and no log is the defective shape; one next to a
  counter has usually been thought about.

  This rule exists because four blanket suppressions were found in a single
  session and three shared one failure mode: **the system keeps working, quietly
  degraded, with no signal.** A dropped `LISTEN` connection ended cross-worker
  fan-out for the process lifetime; a database down at boot left no doorbell task
  spawned at all; a pass whose first shift never enqueued was simply never
  driven. Note the trap in the first of those — `Connection.notifications()`
  *returns* rather than raises on close, so the loop died without any exception
  and the `suppress` was catching nothing. **A site is not safe merely because
  nothing appears to raise there.**

  Legitimate cases exist and should say so in place: best-effort cleanup, a
  fire-and-forget publish where the row already committed (`progress` and
  `_orm_events` swallow deliberately; `rooms` does not, because its caller awaits
  the fan-out), and a connection boundary in the server where one bad peer must
  not stop the process. A supervisor, an accept loop, or a startup path is never
  one of these.
- **`typing.Union is types.UnionType` on 3.14.** They were unified, so a clause
  testing both is the same test written twice. Two such clauses have been deleted
  as dead code after a mutant survived on them.
- **A decorator annotated `(cls: type) -> type` erases the class**, so a
  synthesised `__init__` becomes unknown and a nested declaration becomes an
  invalid type form. `@typing.dataclass_transform(field_specifiers=(...))` plus
  `def deco[T](cls: type[T]) -> type[T]` fixes it. This stays hidden while the
  decorator is only used from `tests/`, because `[tool.ty.src] include = ["src"]`
  — the first *source* module to use it is where `ty` finally objects.
- **A handler's return value must subclass one of the response classes.**
  `app.py`'s coercion ends in a closed `isinstance` check, so a duck-typed object
  with a correct `__call__(send)` dies with `handlers must return a
  response-compatible value`. Subclassing `StreamingResponse` also picks up the
  deferred-cleanup contract that releases a borrowed database connection.
- **`tenant: Query[str]` is a bug, not a spelling.** It produces no query
  parameter *and* silently retypes the path parameter. Write
  `Annotated[str, Query()]` at module scope.

## Source rules

- Keep public modules, tests, and examples self-explanatory and current.
- **The brand may be poetic; the API must stay literal.** Use plain,
  conventional technical names. Never theme a technical term.
- Keep examples runnable on Python 3.14 and distinguish Wreath-native behavior
  from portable ASGI behavior.

## Current scope

Wreath ships HTTP/1.1, HTTP/2, and an optional HTTP/3 build; binding and
validation; OpenAPI and typed client generation; dependencies; the middleware
pipeline and its built-ins; the PostgreSQL driver, ORM, and migration stack;
durable jobs and messaging; authentication and a built-in Cedar authorizer;
first-class structured logging on the Flight Recorder's ring; and a native
documentation site generator. Treat those as current features.
