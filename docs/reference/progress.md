# `wreath.progress`

Report how far a long-running task has got, and expose that over SSE, a
WebSocket, or a plain JSON status endpoint. The task writes through a
`ProgressReporter`; `status_response` and `progress_stream` read.

The registry is in-process, which is exactly wrong for the case that matters
most — the durable job runs on one worker and the browser is connected to
another. Give `ProgressRegistry` a message bus and every report reaches every
worker, so whichever one holds the stream can answer it. The guide is
[Progress reporting](../guides/progress.md).

::: wreath.progress
