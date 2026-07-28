# `wreath.recording`

Two halves of one subsystem, reached for at opposite ends of a bad day.

**Capture policy**, which is most of this module: deny-by-default value types
describing what a Forensic-mode recorder may retain, validated so a policy
cannot exceed its own bounds. The never-capture field classes cannot be enabled
through this API at all — that is structural, not a default. Capture itself
ships: the native recorder arms, triggers, redacts, and writes `WFR1`.

**Crash forensics**, which is `read_ring_file`. Given
`TelemetryConfig.ring_path`, the recorder maps its ring from a file instead of
the heap, so a process that dies badly leaves its last records readable; this
reads them back. It reports what it could not recover rather than raising,
because a file recovered from a crash is where a strict reader is least useful.
`wreath flight read <path>` is the same thing from a terminal.

This is not durability. The mapping survives the *process* — a segfault, a
`SIGKILL`, an `abort()` — because the pages belong to the kernel. It does not
survive a machine losing power before they are written back. A clean shutdown
`msync`s; nothing else does.

::: wreath.recording
