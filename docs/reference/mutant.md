---
keywords: mutation testing, test quality, authorization tests, coverage, mutmut, cosmic-ray, control removal, security testing
---
# `wreath.mutant`

Mutation testing that mutates the *controls you declared*, not the lines you
wrote. It removes one at a time — a clause from an authorization predicate, a
`raise` that refuses, a Cedar `forbid`, a withheld field set, a rate limit's key
— and re-runs the tests that reach it. A control whose removal nothing noticed
is a finding; a control no test reaches at all is a different finding, reported
separately.

Reach for it when you want to know whether a test suite is *watching* something
rather than merely running it. See [Would your tests notice?](../guides/mutant.md)
for the idea, the two real findings that motivated it, and — in more detail than
here — what the tool cannot see.

It exists because `mutmut` and `cosmic-ray` have no idea what an
`AuthRequirement` is. A general mutation tester flips comparisons and deletes
statements, so its budget goes to arithmetic and its score is dominated by
mutants nobody would ship. Wreath owns the declarations, so `wreath mutant` can
phrase every mutation as a sentence from a post-mortem: *the role check was
dropped*, *the refusal never fired*, *the withheld column became writable*, *the
ceiling was keyed on something the caller mints for free*. That scoping is also
what keeps the equivalent-mutant problem tractable — deleting a `raise` that
refuses a request is seldom semantically equivalent — and where the tool cannot
tell, it says `undecided` rather than `survived`.

## The run

`wreath mutant` imports every module it will mutate, so the operators can ask
live objects real questions instead of guessing from syntax — *does this keyword
have a default? is this constant a compiled pattern?* — and declines any
mutation it cannot build, before a single test runs. It then collects the suite
once to warm the interpreter, takes one instrumented baseline under
`sys.monitoring` (PEP 669), and runs one `fork()` per mutant against only the
tests the baseline saw execute that line.

A mutant's own run stops at the first failure — it only has to be caught once,
and the report needs one name to be actionable, so a control most of the suite
reaches costs "run until something objects" rather than "run 874 tests". The
baseline never does that: there, every result counts.

Nothing is ever written to your source tree. The mutated module is compiled in
the parent and the one changed code object is assigned over the live function's
`__code__` in the child, which is total: every `from x import y` alias bound
long before the mutation sees it, because they all point at the same function
object. An import-time constant — a bound, a redaction pattern, a deny-list —
has no function to recompile, so it is rebound by value in the defining module
and in every module that imported it, matched by identity rather than equality.

Two consequences are worth knowing. The patch target is the **outermost**
enclosing function, not the innermost, because a handler defined inside a router
factory has no reachable function object of its own; recompiling the factory
means the next call to it builds the mutated handler, which is when a test
constructs its app. And a mutant is scored `KILLED` only when a test that
*passed at baseline* fails — an already-broken test can never catch anything,
and counting it would report safety that is not there.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `killed` | A baseline-passing test failed with the control removed. The suite is watching it. |
| `survived` | Every test that reaches the line still passed. A question, not a verdict. |
| `unreached` | No test in the baseline executed the line. ADR 0024's shape in its purest form. |
| `equivalent` | The mutation compiles to identical bytecode. Provably not a finding. |
| `timeout` | The mutant exceeded `--timeout`. Undecided, and said so. |
| `error` | This tool could not build or apply the mutant. Its fault, not yours, and counted where you can see it. |

## Operators

Filter with `--operators PREFIX` (repeatable; matches on the prefix, so
`--operators guard` selects all four `guard.*`).

| Operator | Removes |
| --- | --- |
| `predicate.drop-operand` | one clause from a compound `and`/`or` condition |
| `predicate.always-true` | every check in a function whose name reads as a permission question (`_owns`, `is_allowed`, `verify_*`) |
| `expression.take-branch` | the choice in a conditional expression — a limit keyed on the wrong side |
| `comprehension.drop-clause` | the filter on a comprehension, so a withheld-field set stops withholding |
| `guard.remove-raise` | a `raise` that refuses |
| `guard.never-fires` | a guarded branch, by making its condition `False` |
| `guard.always-fires` | the condition on a guarded branch, by making it `True` |
| `guard.drop-statement` | a call that establishes who the caller is, before a later check reads it |
| `declaration.drop-keyword` | a control keyword at a call site — `authenticated=`, `second_factor=`, `policies=`, `dependencies=`, `middleware=`, `rate_limit=`, `readonly=`, `expose=`, `audience=`, `origins=`, `algorithms=`, `require_user_verification=`, and the rest of `CONTROL_KEYWORDS` |
| `declaration.widen-bound` | a numeric bound at a call site, widened past reach |
| `crud.drop-operation-authorize` | one operation's entry in an `authorize={...}` mapping, so that operation alone falls back |
| `crud.widen-access` | one operation's rule, rewritten to the permissive twin of whatever it names — `Access.deny()` becomes `Access.public()` |
| `crud.unprotect-column` | one column from `readonly=`/`exclude=`, so that column alone loses its protection |
| `cedar.flip-effect` | a Cedar `forbid`, turned into a `permit` |
| `cedar.drop-condition` | a `when` or `unless` clause from a Cedar policy |
| `cedar.delete-policy` | one whole policy statement |
| `value.widen-bound` | a module-level numeric ceiling |
| `value.disable-pattern` | a module-level redaction pattern, replaced by one that matches nothing |
| `value.empty-denylist` | every entry in a module-level deny-list |

`declaration.drop-keyword` only offers a keyword the callee genuinely has a
default for, checked against the live signature. A mutation that raises
`TypeError` would be caught by any test that ran it, and a broken mutant scored
as a catch inflates the report with something the suite did not actually notice.
A `**kwargs` callee cannot be asked that question at all, so it is declined —
with one exception, because it is the case this tool was built for: a route
decorator forwards `dependencies=`, `middleware=` and the rest of its metadata
to `RouteDefinition`, and that record *can* be asked. A route built inside a
factory or a fixture therefore has its own declarations mutated; one decorated
at module level ran before anything could patch it, and is not offered.

### Declaring calls the signature cannot answer

Resolution walks attributes to a module global, which succeeds for
`crud_router(...)` imported at the top of a factory and fails for the two
spellings applications actually reach for: `application.crud(...)`, where the
receiver is a *parameter*, and `mcp.tool(...)`, where it is a *local* built
inside the factory. Both declined for every keyword, so the newest
authorization surfaces in the framework went unmutated entirely.

A small table names those call sites and the keywords that are controls on them
— `crud`/`crud_router`, and `tool`/`resource`/`prompt` for MCP. It is consulted
**only after** resolution has already failed, so a callee that can be asked is
still asked and the table never overrides a real signature. It is a heuristic in
the same way `CONTROL_TOKENS` is, and it is the same argument `RouteDefinition`
already makes one layer down: the name and the keyword together are specific
enough to answer without resolving the callee.

### Why an operation gets its own mutant

`authorize={"list": ..., "create": Access.deny()}` is several controls wearing
one keyword. Dropping the keyword removes all of them at once, so any test that
exercises any one operation kills that mutant and the rest are reported as
watched without having been checked — a coarse mutant that dies easily reports
coverage that does not exist, which is the same optimistic-union error
[AGENTS.md](https://github.com/alexogeny/wreath/blob/main/AGENTS.md) records for
native and pure runs, in a different dimension. `crud.drop-operation-authorize`
and `crud.widen-access` take one entry at a time; `crud.unprotect-column` does
the same for `readonly=` and `exclude=`.

`crud.widen-access` reuses the receiver expression the author wrote rather than
building one from a name, so the mutant introduces no identifier the module
might not have imported — `Access.deny()` becomes `Access.public()`, whatever
`Access` was bound to at that call site.

There is no operator for widening `expose=`. `expose` is the escape hatch for
columns crud withholds *by default*, derived from the model's column map, so the
name such a mutation would have to add is precisely the one name not written at
the call site. Fabricating one names a column the model does not have: it either
raises, killing the mutant for a reason unrelated to any control, or is ignored,
reporting a survivor nobody can act on. The wholesale `expose=` drop is still
offered, so the control is not unwatched.

The `predicate.*`, `guard.*`, `expression.*` and `comprehension.*` operators are
confined to functions whose own source names a control — `CONTROL_TOKENS` is
that vocabulary, and it is a heuristic rather than an analysis. It is why
`wreath mutant` does not offer to mutate your JSON encoder, and it is also why a
control named in words the list does not know will be skipped. Both lists are
public and importable; extend them in a `conftest` if your domain has its own
words for refusing.

## Command line

```bash
wreath mutant                                  # this project, its tests
wreath mutant --path src/shop/policies.py      # one file
wreath mutant --operators cedar declaration    # two families
wreath mutant --only ':88'                     # one mutant, by id
wreath mutant --changed HEAD                   # only what you just wrote
wreath mutant --format json > mutants.json     # the whole report, machine-readable
```

| Flag | Effect |
| --- | --- |
| `--path PATH` | file or directory to mutate (repeatable). Defaults to `src/`, else the project's own packages. |
| `--tests PATH` | test path for pytest (repeatable). Defaults to `tests/`. |
| `--operators PREFIX` | only operators starting with `PREFIX` (repeatable). |
| `--only TEXT` | only mutants whose id contains `TEXT`. An id is `operator@path:line`, with `#1`, `#2` … for several at one line. |
| `--pytest-arg ARG` | extra argument for every pytest invocation (repeatable). Spell it with an `=` when the argument itself starts with a dash — `--pytest-arg=--ignore=tests/slow.py` — or `argparse` reads it as a flag of its own. |
| `--timeout SECONDS` | per-mutant deadline; an overrun is `undecided`. Default 60. |
| `--max-candidates N` | decline a mutant that would run more than `N` tests. Default 4000. |
| `--limit N` | stop after `N` mutants **in line order** — a smoke test of the setup, not a way to reach new code. |
| `--changed REF` | only mutants on lines that differ from `REF`. Untracked `.py` files count entirely. Composes with `--limit`. |
| `--verbose` | also list what was killed, and which test caught each. |
| `--quiet` | no per-mutant progress on stderr. |
| `--fail-on-survivor` | exit 1 when anything survived or went unreached. |

### Bounding a pass onto code you just wrote

`--limit N` takes the first `N` mutants **in line order**, so a bound of 40 over
a 1500-line module spends its whole budget at the top of the file. New work is
appended, which made the only bound the tool had unable to reach the one code a
run was usually about: a real pass reported `0 killed / 3 survived / 37
unreached` describing a function nobody had touched. `--path` cannot address
part of a file, so it is no help either.

`--changed REF` is the answer to that workflow. It restricts candidates to lines
that differ from a git ref — `--changed HEAD` for uncommitted work, `--changed
main` for a branch — and composes with `--limit` when the diff is itself large.

`--only` is for a single known mutant and it is worth being precise about,
because a plausible misuse selects nothing. The line in `operator@path:line` is
where the **operator anchors**, not where the control reads: an operand inside a
compound condition carries the operand's line, and a keyword in a declaration
carries the keyword's *value* line. Reading four decision lines off the source
and passing them to `--only` therefore matches zero. Run once without a selector
and copy an id out of the report.

**A selector that matches nothing is now refused with exit 2** rather than
reporting `0 killed, 0 survived` and exiting 0. A bound that silently selects
nothing is a check that passes because it has nothing to check — the same shape
ADR 0024 names, one level up — and it cost one agent a whole pass that described
unrelated code.

### The one module this tool cannot measure

`wreath._mutant` is excluded from mutation unconditionally. Mutating the
operator library while it is generating mutants is incoherent — the mutated
operators would decide what to mutate — so the runner skips any module whose
name starts with `wreath._mutant`. The consequence is worth stating plainly:
**this tool is the one part of Wreath it cannot report on**, and its own
correctness rests on `tests/test_mutant.py` instead. Do not spend time trying to
point it at itself; it will find zero files and, with a selector, now say so.

**It exits 0 by default, and it is not part of `wreath check`.** That is
`wreath-dup-scan`'s posture and it is deliberate: plenty of survivors are
legitimate — a defence behind another defence, a branch your deployment cannot
reach — and a gate that cries wolf trains everyone to stop reading it. Reach for
`--fail-on-survivor` when you have a named list of modules you have already
cleared and want kept clear.

Linux-first: the runner uses `os.fork()`, which is what makes a mutant cost a
process rather than a full import graph.

::: wreath.mutant
