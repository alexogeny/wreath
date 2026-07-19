# `wreath.inspector`

Read-only runtime inspection for the Native Flight Recorder: a local
Unix-socket protocol served inside the application process, a small client,
and the `wreath inspect` CLI built on it. The Inspector is off unless an
`InspectorConfig` is passed to the server; it never binds a TCP port.

::: wreath.inspector
