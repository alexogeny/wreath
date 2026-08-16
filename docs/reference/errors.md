# `wreath.errors`

Application-owned error reporting. Wreath records unhandled framework-boundary
exceptions on its existing logging/OTLP spine automatically; reporters add an
external sink without replacing responses or taking ownership of vendor-global
configuration. First-party adapters cover Sentry, Rollbar, and Bugsnag; the
native OTLP/logging path remains the vendor-neutral default.

::: wreath.errors
