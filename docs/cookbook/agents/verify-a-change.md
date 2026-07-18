# Verify a change

Green tests are necessary, not sufficient. For any change with a runtime surface,
drive the flow it affects and watch what happens — don't infer from the diff.

1. **Exercise it in process** with `TestClient`, hitting the real route so the
   request travels the whole path — middleware, authentication, binding — not
   just the function you edited.
2. **Check both twins** when you've touched an accelerated path. Run with the
   native extension built, and again with `WREATH_PURE=1`. They must agree; a
   disagreement is a bug in one of them.
3. **Price the boundary** if you were near the request path:
   `uv run wreath-request-trace --check` must not report new Python↔native
   crossings unless you meant to add them (then update the baseline, and say why).
4. **Measure honestly** for performance work — repeated runs, never one sample,
   and render the whole picture with `uv run wreath-bench-report`.

Then report what actually happened. If you skipped a step or a test failed, say
so plainly; a confident summary that hides a failure helps no one.
