# Reserved modules

These public import paths exist but are not implemented yet. They are reserved
so the features can land without a later breaking move. Importing them succeeds;
they export nothing.

| Module | Planned surface |
|---|---|
| `wreath.telemetry` | Native metrics, tracing configuration, and OpenTelemetry integration. |
| `wreath.recording` | Request capture and recording policies. |
| `wreath.replay` | Transport and endpoint-plan replay. |
| `wreath.migrations` | Database migration generation and running over `wreath.orm` / `wreath.postgres`. |
