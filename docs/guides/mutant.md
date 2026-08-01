# Would your tests notice?

A passing test proves your code does what you wrote. It does not prove your test
would notice if it stopped.

Those are different claims, and almost every testing tool measures the first
one. Coverage says a line ran. A green suite says nothing raised. Neither asks
the question that actually matters about a control: *if this refusal were
deleted tomorrow, would anything go red?*

For most code the gap is uncomfortable. For authorization it is where the bugs
live, because a removed control usually makes things **more** successful, not
less. Delete the role check and the request returns `200`. Delete the redaction
and the response has more fields, not fewer. Delete the rate limit and every
call succeeds. A test that asserts "Ada can read her own orders" passes just as
happily in a world where everybody can read everybody's.

`wreath mutant` removes one declared control at a time and re-runs the tests
that reach it. A control whose removal nothing noticed is reported. So is a
control no test reaches at all — separately, because *"the suite would not
notice"* and *"the suite never looks"* are different problems with different
fixes.

```bash
wreath mutant                     # this project, its tests, one report
```

## What it removes

General mutation testers — `mutmut`, `cosmic-ray` — mutate *code*. They flip
`<` to `<=` and delete statements, because they know nothing about the program
they are mutating. Most of what they produce is arithmetic nobody was worried
about, and the score is dominated by it.

Wreath knows what a control is, because you declared one. An `AuthRequirement`,
a Cedar policy, an MCP tool's gate, a CRUD router's withheld field set, a rate
limit's key: these are objects with names, not lines with operators. So the
mutations can be phrased the way an incident report is.

```
SURVIVED -- the control was removed and every test still passed

  src/shop/orders.py
    :88    the refusal `raise Forbidden('not your order')`
           guard.remove-raise in _load_order; 14 test(s) ran it and none objected
    :141   `readonly=` on `crud_router(...)` (it falls back to the default)
           declaration.drop-keyword in build_orders_api; 31 test(s) ran it …

UNREACHED -- no test executes this control at all

  src/shop/policies.py
    :12    a Cedar `forbid` turned into a `permit`
           cedar.flip-effect in <module>
```

Every operator names a control and takes it away:

| Family | What it removes |
| --- | --- |
| `predicate.*` | one clause from an authorization condition; a whole permission check, which now answers `True` |
| `guard.*` | a `raise` that refuses; a guarded branch, taken or skipped; a call that establishes who the caller is |
| `expression.take-branch` | the choice in a conditional expression — a limit keyed on the wrong side |
| `comprehension.drop-clause` | the filter on a withheld-field set, so it stops withholding |
| `declaration.*` | `roles=`, `second_factor=`, `readonly=`, `dependencies=`, `rate_limit=`, `sortable_fields=` … at the call site that declares them; or a numeric bound, widened past reach |
| `cedar.*` | a `forbid` flipped to `permit`; a `when`/`unless` clause dropped; a whole policy deleted |
| `value.*` | a module-level ceiling widened; a redaction pattern that now matches nothing; a deny-list emptied |

`wreath mutant --operators cedar declaration` runs a family at a time.

## Three findings this tool produced on its first real runs

None of these was a guess. All three came out of `wreath mutant` pointed at
Wreath's own suite.

### A rate limit keyed on something the caller mints for free

An MCP tool can carry a per-caller ceiling: *one expensive scan an hour*. The
bucket has to be keyed on something, and the code chose well —

```python
key = principal if principal is not None else session_id
```

— the verified subject when there is one, and the session otherwise, because an
MCP client is usually a gateway and keying on the address collapses every caller
into one bucket. `expression.take-branch` collapses that choice to its
fallback: always the session id.

A model opens a session per turn, and `initialize` is free. A ceiling keyed on
the session is not a ceiling. The mutant was killed — by one test, in one file,
which opens a fresh session inside the loop precisely because that is the attack
— and that is the good outcome: this control is watched, on purpose, by somebody
who thought about it.

The interesting part is what the same run said about the *fix*. The change that
closed this hole also added an eager identity resolution at the top of
`tools/call`, ahead of the charge. `guard.drop-statement` deletes it, and every
test still passed — which reads like dead weight behind a control that was doing
the work anyway, because by the time `tools/call` runs the session-ownership
check has usually resolved the caller already.

It is not dead weight, and this is the paragraph that used to say it was. The
ownership check re-resolves the caller only when the session has a principal to
compare against; a session opened *without* credentials is bound to nobody, so
the check short-circuits and resolves no one. A caller who holds a token and
simply withholds it from `initialize` therefore arrives at `tools/call`
unidentified, and the bucket keys on a session id that `initialize` mints for
free — the original hole, by another door. Every test in the suite authenticated
`initialize` too, so none of them was standing in that doorway.

Read that as the tool working rather than failing. A survivor is a question, and
the answer here was not "delete the line" but "the suite is one shape short":
`test_withholding_credentials_from_initialize_does_not_reset_the_ceiling` now
holds that shape, and the same mutation kills. The two outbound equivalents,
inside `_sampling` and `_elicit`, were the same story one layer down — the
sampling one had a test that *named* it and authenticated every request, so it
could not have noticed.

### A regression test whose setup never created the condition it named

`expose_routes` refuses to expose a route that carries `dependencies=` or
`middleware=`, because a tool call does not replay the route's chain and
exposing one would be a way around what was put in front of it. There is a test
called `test_a_dependency_is_refused_naming_the_route`.

`guard.remove-raise` deletes that refusal, and the test stays green.

It turns out the test declares a handler with `Depends(...)` *in its signature*,
which is refused earlier and for a different reason — a tool call cannot fill a
dependency parameter, so it never reaches the check on the route's
`dependencies=` tuple at all. The name says one control; the setup exercises
another. Two tests elsewhere do cover the real one, so the control was safe; but
the test that most looked like its regression test was not one.

This is ADR 0024's shape — *a check that silently has nothing to check* — and it
is exactly what a mutation report is for. Nobody reading that test would have
seen it. The tool sees it because it does not read names.

The test now declares a clean signature and attaches the guard with
`dependencies=`, so `guard.remove-raise` on that refusal turns it red; the old
body kept its coverage under the name of the control it actually exercises.

### A refusal nothing has ever made fire

`CompositeBackend` tries each authentication backend in turn and the first
identity wins. `guard.never-fires` on that "first identity wins" branch came
back UNREACHED: in 8,037 tests, nothing composes two backends where the first
one answers. So does the refusal in `CompositeBackend.__init__` for an empty
backend list, and the one in `@authorize(action="")` for an empty action.

None of those is a bug today. All of them are code that has never been observed
doing its job, which is a different and cheaper thing to fix than a survivor:
the first test you write there quite often fails, because the refusal was never
right.

## How a run is put together

Three decisions make the difference between a tool people run and a tool people
*mean* to run.

**Test selection with PEP 669.** One instrumented baseline run records which
tests execute which lines, using `sys.monitoring` rather than `sys.settrace`.
Only the lines some operator proposed a mutation for are watched; every other
location retires itself with a single `DISABLE` and costs nothing for the rest
of the run. A mutant then runs only the tests that reach it — often two or three
out of thousands.

**One `fork` per mutant.** The parent imports the application and collects the
suite once. Each mutant is a `fork()` of that warm interpreter, so it costs a
process rather than a full import graph. This is why the tool is Linux-first.

**No file is ever rewritten.** The mutated module is compiled in the parent and
the one changed code object is assigned over the live function's `__code__` in
the child. That is total — every `from x import y` alias bound long before sees
it, because they all point at the same function object — and it means a run
leaves your working tree exactly as it found it, even if you interrupt it.

## Reading the report honestly

**A surviving mutant is a question, not a verdict.** Sometimes the answer is
"no, I don't want a test for that". A `raise` on a branch your application can
never reach, a bound nobody has an opinion about, a defence in depth behind
another defence that *is* tested: all of these survive, and all of them are
fine. The report is a list of things to look at, which is why it exits 0 by
default and is not part of `wreath check`.

**An unreached mutant is usually the more interesting one.** "Every test that
runs this control still passes without it" at least means somebody ran it.
"Nothing in the suite executes this line" means the refusal has never fired in
anger, and a refusal that has never fired is a refusal nobody has watched work.

**A survivor can also mean the control is only ever met by a stand-in.** If
every test that exercises a policy hands it a hand-rolled fake, mutating the
shipped implementation changes nothing any of them assert, and the mutant
survives. That reads as "your tests would not notice", which is exactly right —
ADR 0020 calls the same thing *a double is never more capable than the real
thing*, and it was found here once by hand after thirteen tests passed over a
read that had never worked. When a survivor sits in code you are sure is
covered, look at what the covering tests are actually holding.

**Do not chase the percentage.** The score at the bottom is killed over decided.
It can be driven to 100% by a suite that watches every control this tool knows
how to remove and none of the ones it does not. Read the two lists; the ratio is
a footnote.

## What it cannot see

Being specific here is the point; a tool that overstates its reach is a check
with nothing to check, one level up.

- **Anything not written in Python.** Wreath's native ingress, routing and
  authorization paths are C. `wreath mutant` mutates the Python that declares
  the controls, not the C that enforces some of them.
- **A policy loaded from a file at runtime.** Cedar policies written as a string
  literal in your source are mutated; a `.cedar` file read at startup is out of
  reach, because there is no source construct to rewrite.

    A policy set built at import — `ENGINE = CedarPolicies(POLICY_SOURCE)` — *is*
    reached, and it is the one declaration where that took extra work. The engine
    parses its text once, at construction, so rebinding the string leaves the
    compiled policies in force; the mutation rebuilds the engine as well. An
    engine that is not `CedarPolicies` cannot be asked to recompile and so keeps
    whatever it built.
- **A control declared by data.** A role name in a database row, a limit in an
  environment variable, a policy served by another service: the tool mutates
  what your repository says, not what your deployment says.
- **A declaration that only ever ran at import.** The patch target is the
  outermost enclosing *function*, so a control is mutable where it is built:
  inside a factory, a fixture, a `create_app()`. Tests that construct their app
  per test — the usual shape — are fine. A route decorated at module level is
  not: `@app.get("/x", dependencies=…)` has already run by the time anything
  could be patched, so `dependencies=` there is never offered rather than being
  offered and quietly not installed. The enforcement *inside* your dependency
  is still mutated; it is the declaration that is out of reach. If you want
  your route declarations covered, build them in a function.

    This applies to every declaration, not only routes: `router =
    crud_router(Model, …, authorize={…})` and `@mcp.tool(action=…)` written at
    module level are in exactly the same position, and their controls are not
    offered. Inside a `create_app()`, a `mount()` or a fixture, they are — which
    is where the camera-trap example declares both of its generated routers.
- **A mutation inside a factory nothing calls again.** Same mechanism, other
  end: a factory that ran once at import and was never called again keeps its
  original behaviour, and that mutant will survive for a reason that has
  nothing to do with your tests.
- **Anything a test reaches through a thread, a subprocess or a C frame.** Line
  attribution is per Python frame in the running test. Where it fails, the
  mutant is reported UNREACHED, which is visible, rather than silently scored.
- **Equivalence, in general.** Where a mutation compiles to identical bytecode
  the tool says so and moves on. Where it does not, it does not guess: scoping
  the operators to control removal makes true equivalents rare — deleting a
  `raise` that refuses a request is seldom semantically equivalent — but "rare"
  is not "never", and a survivor you decide is equivalent is a survivor you
  should say so about.

## What a run costs

One complete run, on this repository, over `crud.py`, `pagination.py` and four
files of `_auth/`: **256 mutants in 8 m 46 s**, against a suite of 8,037 tests.

| Phase | Measured |
| --- | --- |
| Plan — parse, scan, and compile every mutant, in the parent, once | 20 ms per mutation |
| Baseline — one instrumented full suite | 113–119 s over nine runs |
| The same suite with no instrumentation | 111 s and 116 s, two runs |
| One mutant | 0.4 s where two tests reach the control; 6 s where 218 do |

Read that as a shape, not a benchmark: it is one machine, and the two
uninstrumented runs differ by more than the instrumentation costs, so the honest
statement is that PEP 669 line attribution did not show up above the noise
rather than that it is free. What generalises is the *ratio*: a baseline is one
suite run, and a mutant costs only the tests that reach the control it removed —
so the cost of a run tracks how widely your controls are exercised, not how
large your suite is. Scope with `--path`, `--operators` or `--only` while you
are iterating, and let the whole thing run when you are not watching it.

Two per-mutant details are worth knowing before you tune `--timeout`. A mutant
stops at the first test that objects, so being *caught* is cheap; **surviving is
what costs**, because a survivor has run every test that reaches it. A control
on the hot path — `AuthRequirement.access_level` is consulted for nearly every
request — therefore takes about as long as your whole suite when it survives,
and reports `undecided` if that exceeds the deadline. The default of 60 s is
sized for that; 20 s is not.

## Where to go next

- [`wreath.mutant`](../reference/mutant.md) — the full surface, every operator,
  every flag.
- [Find the controls your tests do not watch](../cookbook/recipes/find-unwatched-controls.md)
  — the smallest useful run, on your own application.
- [ADR 0024](https://github.com/alexogeny/wreath/blob/main/docs/decisions/0024-a-check-that-has-nothing-to-check.md)
  — the idea this tool automates, and the nine times it was found by hand first.
