# Verify a change

Green tests are necessary, not sufficient. For any change with a runtime surface,
drive the flow it affects and watch what happens — don't infer from the diff.

1. **Exercise it in process** with `TestClient`, hitting the real route so the
   request travels the whole path — middleware, authentication, binding — not
   just the function you edited.
2. **Anchor it outside Wreath** when you've touched an accelerated path. Assert
   what the RFC, the published vectors, or the stdlib says the answer is — not
   what another implementation of ours produces, which agrees happily when both
   are wrong.
3. **Price the boundary** if you were near the request path:
   `uv run wreath-request-trace --check` must not report new Python↔native
   crossings unless you meant to add them (then update the baseline, and say why).
4. **Measure honestly** for performance work — repeated runs, never one sample,
   and render the whole picture with `uv run wreath-bench-report`.
5. **Red-team the failure paths** with replay and fault injection. A happy-path
   test proves the change works when nothing goes wrong; the interesting bugs are
   in what happens when something does. Drive the affected route through
   [`wreath.replay`](../../reference/replay.md) under adversity and assert an
   *owned* outcome:

   - Malformed or truncated input at the wire, via `replay_transport` with a
     `FaultSchedule` — the parser must never crash and never fabricate a `200`.
   - A boundary that fails — a database `SERVER_ERROR`, an outbound
     `READ_TIMEOUT` — via `ReplayAdapters`. The framework must map it to an owned
     status and **release the connection** (`assert not double.leaked`), whatever
     the handler does.
   - Re-run each perturbed case and assert `a.matches(b)`; a non-deterministic
     owned outcome is a bug.

   The recipe [Fuzz your own routes](../recipes/fuzz-your-routes.md) has the
   patterns; `tests/test_replay_faults.py` is the framework's own baseline to copy.

Then report what actually happened. If you skipped a step or a test failed, say
so plainly; a confident summary that hides a failure helps no one.
