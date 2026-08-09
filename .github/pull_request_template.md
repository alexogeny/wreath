<!--
Use whatever tools you like to write this — an editor, a debugger, a model.
None of them go in the commit message, because none of them can answer a
question about the change in six months. You can. That is what authorship means
here, and it is the only thing this asks of you.

CI enforces two mechanical parts of that: `wreath-hygiene` fails on a
co-authorship trailer or a generation notice, and `main`'s ruleset refuses the
push outright. Neither is a judgement about how you worked.
-->

## What this changes

<!-- One or two sentences. What behaviour is different after this merges? -->

## Why

<!-- The problem, not the patch. If it fixes an issue, "Fixes #123". -->

## How it was verified

<!--
Not "tests pass" — which tests, and what would have failed before?

`AGENTS.md` is blunt about this: a tool reporting success is not evidence that
it ran. If you changed behaviour, there should be a test that fails without your
change. Say so here, and say if you watched it fail.

Paste the command you ran:

    uv run wreath test -k <pattern>
-->

## Performance claims

<!--
Delete this section if the change is not about speed.

If it is: what did you measure, on what, how many times, and against what
baseline? A single run is not a result. Removing a piece and timing the whole
operation beats a profiler whose per-call overhead exceeds what you are
measuring. See AGENTS.md.
-->

---

- [ ] I am the author of this change and can explain and defend every line of it.
- [ ] No co-authorship trailers, generation notices, or tool attribution.
- [ ] Tests cover the new behaviour, and I saw the new test fail before it passed.
- [ ] `uv run wreath-check` passes, or I have said below which gate does not and why.
